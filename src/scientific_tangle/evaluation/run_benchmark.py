"""Запуск benchmark оценки retrieval качества на реальных документах.

Сравнивает hybrid retrieval (BM25 + evidence matching + entity boost)
с lexical baseline (BM25 only) на gold-кейсах из реальных документов
корпуса «Источники информации/Обзоры».

Это гейтируемый CLI, а не просто отчёт. Коды выхода:

* ``0`` — измерение выполнено и критерии приёмки подтверждены;
* ``1`` — измерение выполнено, но критерии приёмки не выполнены либо найдена
  регрессия относительно эталона (``--baseline``);
* ``2`` — измерение НЕ выполнено: нет корпуса, нет манифеста, корпуса неполного
  или не извлечено ни одной находки. Отчёт о метриках при этом не печатается,
  чтобы недоступный корпус нельзя было прочитать как «всё хорошо».
* ``3`` — команда собрана неверно: неизвестный или двусмысленный флаг, отсутствующее
  значение. Отдельный код нужен потому, что argparse при ошибке разбора выходит
  с кодом 2, а его CI читает ровно как «измерение не выполнено»: опечатка в имени
  флага обвиняла бы монтирование ``/data/sources``, и чинить стали бы не тот код.
  ``--help`` при этом остаётся кодом 0.

Пороги приёмки снимаются флагами (значения по умолчанию — текущие нормы):
``--min-recall-at-10`` (0.90), ``--max-p95-seconds`` (3.0),
``--require-hybrid-beats-lexical`` (по умолчанию требовать), ``--max-regression``
(5 % относительного падения headline-метрики). ``--json-out`` пишет сырые метрики
для архива CI, ``--write-baseline`` сохраняет эталон, ``--baseline`` сравнивает
текущий прогон с прошлым.

Эталон обязателен для гейта, а не опциональна украшением: он пишется только
принятым прогоном на полном корпусе (``--write-baseline``), кладётся в репозиторий
или в CI-артефакт, и путь к нему задаётся переменной ``BENCHMARK_BASELINE`` в
``.gitlab-ci.yml``. Пока эталон не установлен, CI-гейт называется явным
неустановленным (job ``benchmark:baseline-gate``), а не зелёным: сравнения нет,
и притворяться проверкой оно не вправе.

Выборка и честность чисел
-------------------------
Каждое агрегатное число обязано называть, по сколько кейсов в него вошло:
``aggregate`` делит по числу кейсов с самой метрикой (а не по первому ключу),
``aggregate_samples`` и поле ``samples`` у критерия приёмки показывают этот n, а
``latency_stats`` возвращает ``LatencySample``: ниже ``MIN_SAMPLES_FOR_P95`` = 5
замеров перцентиль не считается, в ``p95_seconds`` кладётся наихудшее наблюдение
(консервативнее перцентиля) и ``p95_is_percentile: false`` — «недостаточная
выборка» вместо псевдостатистики.

Регрессионная проверка воспроизводит форму A/B-сравнения из
``api/app.py`` (``run_evolution_experiment``): метрики качества не должны падать,
латентность не должна расти, а кейсы, проходившие в эталоне и не прошедшие
сейчас, перечисляются явно.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

# Добавляем src в путь для запуска как модуля или скрипта
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Импорты пакета идут после бутстрапа sys.path, иначе скрипт не запускается из
# checkout без установки пакета.
from scientific_tangle.domain.contracts import DocumentRequest, Finding  # noqa: E402
from scientific_tangle.domain.models import EvidenceLocator  # noqa: E402
from scientific_tangle.evaluation.gold_cases import (  # noqa: E402
    REAL_GOLD_CASES,
    GoldCaseReconciliation,
    reconcile_gold_cases,
)
from scientific_tangle.services.document_parser import parse_document  # noqa: E402
from scientific_tangle.services.knowledge import stable_uuid  # noqa: E402

# ── Константы ──────────────────────────────────────────────────────────

# Определяем пути к данным: в Docker используем SOURCE_ROOT из окружения,
# при локальном запуске — структуру каталогов проекта
_env_source = os.environ.get("SOURCE_ROOT")
if _env_source and Path(_env_source).exists():
    SOURCE_ROOT = Path(_env_source)
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]
    SOURCE_ROOT = PROJECT_ROOT / "Источники информации"

# Манифест может быть рядом с пакетом (локальный запуск) или в /app/src (Docker)
_MANIFEST_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "preload_manifest.json",
    Path("/app/src/scientific_tangle/preload_manifest.json"),
    Path(__file__).resolve().parents[3] / "src" / "scientific_tangle" / "preload_manifest.json",
]
MANIFEST_PATH = next((p for p in _MANIFEST_CANDIDATES if p.exists()), _MANIFEST_CANDIDATES[0])

MAX_FINDINGS_PER_DOC = 25
TOP_K_VALUES = [3, 5, 10]

# ── Пороги приёмки и коды выхода ───────────────────────────────────────

MIN_RECALL_AT_10 = 0.90
MAX_P95_SECONDS = 3.0
MAX_RELATIVE_REGRESSION = 0.05
# Ниже этой выборки перцентиль не является перцентилем: p95 из трёх замеров —
# максимум под чужим именем, и отчёт обязан об этом сказать.
MIN_SAMPLES_FOR_P95 = 5

EXIT_OK = 0
EXIT_NOT_ACCEPTED = 1
EXIT_NOT_MEASURED = 2
# Ошибка разбора команды живёт отдельно от «измерение не выполнено»: см. коды выхода
# в докстринге модуля.
EXIT_USAGE = 3

PAYLOAD_VERSION = 1
# Головные метрики, у которых «меньше — лучше»: для них считается рост, а не падение.
LOWER_IS_BETTER = frozenset({"p95_seconds", "average_seconds"})
# Падение относительно нуля выразить нельзя, поэтому baseline == 0 не сравнивается.
_EPSILON = 1e-12

# ── Технические ключевые слова для фильтрации и entity boost ───────────

TECH_KEYWORDS = frozenset(
    {
        # Элементы (русские и латинские)
        "медь",
        "cu",
        "никель",
        "ni",
        "кобальт",
        "co",
        "железо",
        "fe",
        "свинец",
        "pb",
        "золото",
        "au",
        "серебро",
        "ag",
        "платина",
        "pt",
        "палладий",
        "pd",
        "литий",
        "li",
        "цинк",
        "zn",
        "висмут",
        # Процессы
        "выщелачивание",
        "флотация",
        "обжиг",
        "электролиз",
        "электроэкстракция",
        "экстракция",
        "осаждение",
        "спекание",
        "конвертирование",
        "плавка",
        "рафинирование",
        "очистка",
        "восстановление",
        "окисление",
        "кучное",
        "хлорное",
        "цианидное",
        "автоклавное",
        "сульфатизирующий",
        # Материалы
        "штейн",
        "файнштейн",
        "шлак",
        "матт",
        "концентрат",
        "руда",
        "раствор",
        "электролит",
        "магнетит",
        "гематит",
        "ярозит",
        "гетит",
        "сподумен",
        "рассол",
        "сульфат",
        "хлорид",
        "цианид",
        "оксид",
        "оксигидроксид",
        "пирит",
        "халькопирит",
        "пентландит",
        "малахит",
        "азурит",
        "халькозин",
        "борнит",
        "ковеллин",
        # Оборудование
        "конвертер",
        "печь",
        "автоклав",
        "электропечь",
        "катод",
        "анод",
        "реактор",
        "фильтр",
        "горелка",
        "электрод",
        # Заводы и проекты
        "nikkelverk",
        "niihama",
        "sandouville",
        "panton",
        "albion",
        "михеевское",
        "томинское",
        "садбери",
        "sumitomo",
        "terrafame",
        "tpox",
        "galvanox",
        "cesl",
        "platsol",
        "hybinette",
        "хибинетт",
        "outotec",
        "neomet",
        "brixlegg",
        "hecla",
        # Прочее
        "мпг",
        "извлечение",
        "содержание",
        "температура",
        "давление",
        "степень",
        "выход",
        "потери",
        "батарей",
        "промпродукт",
    }
)


# ── Стеммизация (упрощённая для русского языка) ─────────────────────

_STEM_SUFFIXES = sorted(
    (
        "ться",
        "тся",
        "ость",
        "ение",
        "ого",
        "его",
        "ому",
        "ему",
        "ыми",
        "ими",
        "ами",
        "ями",
        "ая",
        "яя",
        "ой",
        "ей",
        "ое",
        "ее",
        "ые",
        "ие",
        "ых",
        "их",
        "ах",
        "ях",
        "ам",
        "ям",
        "ов",
        "ев",
        "ом",
        "ем",
        "ью",
        "ия",
        "ть",
        "шь",
        "ет",
        "ют",
        "ит",
        "ят",
        "а",
        "я",
        "о",
        "е",
        "ы",
        "и",
        "у",
        "ю",
        "ь",
    ),
    key=len,
    reverse=True,
)


def _stem(word: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


# ── Токенизация + стеммизация ─────────────────────────────────────────


def tokenize(text: str) -> list[str]:
    """Токенизация с упрощённой стеммизацией для русского языка."""
    text = text.lower()
    tokens = re.findall(r"[а-яёa-z0-9]+(?:[-][а-яёa-z0-9]+)?", text)
    return [_stem(t) for t in tokens if len(t) >= 3]


# ── BM25 индекс ────────────────────────────────────────────────────────


class BM25Index:
    """BM25 ретривер для скоринга документов по запросу."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / max(self.n, 1)
        self.tf = [Counter(doc) for doc in corpus]
        self.df: Counter[str] = Counter()
        for tf in self.tf:
            for term in tf:
                self.df[term] += 1

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1)

    def score(self, query_tokens: list[str], doc_idx: int) -> float:
        tf = self.tf[doc_idx]
        dl = self.doc_len[doc_idx]
        norm = self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1))
        total = 0.0
        for term in query_tokens:
            if term in tf:
                f = tf[term]
                total += self.idf(term) * (f * (self.k1 + 1)) / (f + norm)
        return total


# ── Извлечение находок из документов ───────────────────────────────────


def _split_sentences(text: str) -> list[str]:
    """Разбивает текст на предложения."""
    text = re.sub(r"\s+", " ", text)
    raw = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z0-9])", text)
    return [s.strip() for s in raw if s.strip()]


def _count_tech_keywords(text: str) -> int:
    """Считает технические ключевые слова с учётом русских словоформ."""
    text_lower = text.lower()
    count = 0
    for kw in TECH_KEYWORDS:
        if kw in text_lower:
            count += 1
        elif len(kw) >= 5 and kw[:5] in text_lower:
            count += 1
    return count


def _is_technical(sentence: str) -> bool:
    if not (30 <= len(sentence) <= 500):
        return False
    return _count_tech_keywords(sentence) >= 2


def _compute_confidence(sentence: str) -> float:
    score = 0.5
    if re.search(r"\d+[,.\s]?\d*", sentence):
        score += 0.1
    if "%" in sentence:
        score += 0.05
    if re.search(r"[A-Z][a-z]?\d?", sentence):
        score += 0.05
    score += min(_count_tech_keywords(sentence) * 0.02, 0.15)
    return min(score, 0.95)


def _extract_subject(sentence: str) -> str:
    s = sentence.lower()
    for kw in (
        "выщелачивание",
        "флотация",
        "обжиг",
        "электролиз",
        "электроэкстракция",
        "экстракция",
        "осаждение",
        "спекание",
        "штейн",
        "файнштейн",
        "шлак",
        "раствор",
        "сподумен",
        "медь",
        "никель",
        "кобальт",
        "железо",
        "литий",
        "свинец",
        "цианид",
        "магнетит",
        "гематит",
    ):
        if kw in s:
            return kw
    return "процесс"


def extract_findings_from_document(doc: DocumentRequest) -> list[Finding]:
    """Извлекает findings из документа: разбор предложений + фильтр."""
    findings: list[Finding] = []
    counter = 0
    doc_id = stable_uuid(doc.title)

    for fragment in doc.fragments:
        fragment_text = fragment.text
        for sent in _split_sentences(fragment_text):
            if not _is_technical(sent):
                continue
            page = fragment.page if fragment.page else 1
            # evidence.quote — контекст фрагмента для семантического matching
            quote = fragment_text[:500]
            if len(quote) < 20:
                quote = sent
            finding = Finding(
                id=f"finding-{doc.title}-{counter:04d}",
                statement=sent,
                confidence=_compute_confidence(sent),
                evidence=[
                    EvidenceLocator(
                        document_id=doc_id,
                        source_title=doc.title,
                        page=page,
                        quote=quote,
                    )
                ],
                subject=_extract_subject(sent),
                predicate="DESCRIBES",
            )
            findings.append(finding)
            counter += 1

    # Ограничиваем количество findings на документ
    if len(findings) > MAX_FINDINGS_PER_DOC:
        findings.sort(key=lambda f: f.confidence, reverse=True)
        findings = findings[:MAX_FINDINGS_PER_DOC]
    return findings


# ── Retrieval ─────────────────────────────────────────────────────────


def lexical_retrieval(
    query: str,
    findings: list[Finding],
    bm25_stmt: BM25Index,
    top_k: int,
) -> list[Finding]:
    """Лексический baseline: BM25 только по statement."""
    q_tokens = tokenize(query)
    scores = [(bm25_stmt.score(q_tokens, i), i) for i in range(len(findings))]
    scores.sort(reverse=True)
    return [findings[idx] for _, idx in scores[:top_k]]


def hybrid_retrieval(
    query: str,
    findings: list[Finding],
    bm25_stmt: BM25Index,
    bm25_ev: BM25Index,
    top_k: int,
) -> list[Finding]:
    """Hybrid: BM25(statement) + BM25(evidence) + document boost + entity boost.

    Имитирует три компонента полноценного GraphRAG:
    1. Лексическая — BM25 по statement (как baseline)
    2. Семантическая — BM25 по evidence context (фрагмент документа)
    3. Графовая — boost по совпадению заголовка документа и entity overlap
    """
    q_tokens = tokenize(query)
    q_long = [t for t in q_tokens if len(t) >= 4]
    # Технические токены запроса не зависят от finding: считаем один раз,
    # а не на каждый документ.
    q_tech_tokens = set()
    for t in q_long:
        for kw in TECH_KEYWORDS:
            if kw in t or t in kw:
                q_tech_tokens.add(t)
                break

    scores: list[tuple[float, int]] = []
    for i in range(len(findings)):
        f = findings[i]
        # 1) BM25 по statement
        stmt = bm25_stmt.score(q_tokens, i)
        # 2) BM25 по evidence (семантический контекст фрагмента)
        ev = bm25_ev.score(q_tokens, i)
        # 3) Document title boost — графовая компонента:
        #    4-символьные префиксы токенов запроса в заголовке документа
        source_lower = _source(f).lower()
        title_overlap = sum(1 for qt in q_long if qt[:4] in source_lower)
        doc_boost = title_overlap * 0.2
        # 4) Entity boost — пересечение технических токенов (substring matching)
        f_tokens = set(tokenize(f.statement))
        entity_boost = len(q_tech_tokens & f_tokens) * 0.5 * f.confidence
        total = stmt + 0.3 * ev + doc_boost + entity_boost
        scores.append((total, i))

    scores.sort(reverse=True)
    return [findings[idx] for _, idx in scores[:top_k]]


# ── Метрики ────────────────────────────────────────────────────────────


def _source(finding: Finding) -> str:
    if finding.evidence:
        return finding.evidence[0].source_title
    return ""


def compute_case_metrics(
    retrieved: list[Finding],
    expected: set[str],
    top_k_values: list[int],
) -> dict[str, float]:
    """Вычисляет recall@k, hit@k, precision@k, MRR и NDCG@3 для одного кейса.

    recall@k — доля ожидаемых источников, найденных в top-k. Прежняя реализация
    клала в эту метрику «найдён ли хотя бы один» (то есть hit-rate) и тем самым
    завышала результат ровно вдвое на кейсах с двумя источниками.
    """
    results: dict[str, float] = {}
    total_expected = len(expected)
    for k in top_k_values:
        top_k = retrieved[:k]
        rel = [1 if _source(f) in expected else 0 for f in top_k]
        found = len({_source(f) for f in top_k} & expected)
        results[f"recall@{k}"] = found / total_expected if total_expected else 0.0
        # Отдельная метрика, а не переименование: порог приёмки исторически
        # смотрит на «хоть что-то нашлось», и это надо видеть рядом с recall.
        results[f"hit@{k}"] = 1.0 if found else 0.0
        if k in (3, 5):
            results[f"precision@{k}"] = sum(rel) / k
    # MRR
    first_rank = next((i for i, f in enumerate(retrieved, 1) if _source(f) in expected), None)
    results["mrr"] = 1.0 / first_rank if first_rank else 0.0
    # NDCG@3: идеал строится по известным релевантным источникам, а не по тем,
    # что случайно попали в top-3, — иначе промах не снижал оценку вообще.
    rel3 = [1 if _source(f) in expected else 0 for f in retrieved[:3]]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel3))
    ideal_hits = min(total_expected, 3)
    idcg = sum(1 / math.log2(i + 2) for i in range(ideal_hits))
    results["ndcg@3"] = dcg / idcg if idcg > 0 else 0.0
    return results


def aggregate(case_metrics: list[dict[str, float]]) -> dict[str, float]:
    """Среднее по каждой метрике — только по тем кейсам, где она реально посчитана.

    Прежняя версия брала ключи первого кейса и делила сумму на ``len(case_metrics)``:
    отсутствующий ключ ронял прогон на KeyError, а метрика, посчитанная не у всех
    кейсов, получала в числителе ноль вместо «нет значения» и занижала среднее.
    Теперь ключи — объединение, знаменатель — число кейсов с этой метрикой
    (оно видно в ``aggregate_samples``).
    """
    if not case_metrics:
        return {}
    measured: dict[str, list[float]] = {}
    for metrics in case_metrics:
        for key, value in metrics.items():
            measured.setdefault(key, []).append(value)
    return {
        key: round(sum(values) / len(values), 3)
        for key, values in sorted(measured.items())
        if values
    }


def aggregate_samples(case_metrics: list[dict[str, float]]) -> dict[str, int]:
    """Сколько кейсов дали значение каждой метрики: среднее без n не статистика."""
    samples: dict[str, int] = {}
    for metrics in case_metrics:
        for key in metrics:
            samples[key] = samples.get(key, 0) + 1
    return samples


# ── Головные метрики, пороги и эталон ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class LatencySample:
    """Замер латентности с размером выборки: «p95» без n нельзя сравнить с порогом."""

    average: float
    p95: float
    samples: int
    percentile_measured: bool

    @property
    def note(self) -> str:
        if not self.samples:
            return "латентность не измерена: ни одного замера"
        if not self.percentile_measured:
            return (
                f"p95 не измерен (недостаточная выборка n={self.samples} < "
                f"{MIN_SAMPLES_FOR_P95}): показан наихудший наблюдаемый замер"
            )
        return f"p95 по n={self.samples} замеров"


def latency_stats(latencies: list[float]) -> LatencySample:
    """Среднее, «p95» и размер выборки в секундах.

    ``p95`` из трёх замеров — не перцентиль: ниже ``MIN_SAMPLES_FOR_P95`` отдаём
    наихудшее наблюдение (консервативнее перцентиля, поэтому порогу он не помогает)
    и явную отметку ``percentile_measured=False``, чтобы отчёт не врал про хвост.
    """
    samples = len(latencies)
    average = sum(latencies) / samples if samples else 0.0
    ordered = sorted(latencies)
    if not samples:
        return LatencySample(average=0.0, p95=0.0, samples=0, percentile_measured=False)
    if samples < MIN_SAMPLES_FOR_P95:
        return LatencySample(
            average=average,
            p95=ordered[-1],
            samples=samples,
            percentile_measured=False,
        )
    index = min(round((samples - 1) * 0.95), samples - 1)
    return LatencySample(
        average=average, p95=ordered[index], samples=samples, percentile_measured=True
    )



def headline_metrics(
    hybrid: dict[str, float],
    lexical: dict[str, float],
    latencies: list[float],
) -> dict[str, float]:
    """Плоская карта метрик, по которой сверяется эталон."""
    stats = latency_stats(latencies)
    metrics = {f"hybrid_{key}": value for key, value in hybrid.items()}
    metrics.update({f"lexical_{key}": value for key, value in lexical.items()})
    metrics["average_seconds"] = round(stats.average, 6)
    metrics["p95_seconds"] = round(stats.p95, 6)
    return metrics


def passing_cases(case_details: list[dict[str, Any]]) -> dict[str, bool]:
    """Кейсы, в которых весь ожидаемый источник попал в top-10 (отметка ✓)."""
    return {str(case["query"]): bool(case["hybrid_recall"] >= 1.0) for case in case_details}


def evaluate_acceptance(
    hybrid: dict[str, float],
    lexical: dict[str, float],
    latencies: list[float],
    thresholds: argparse.Namespace,
    case_count: int = 0,
) -> dict[str, dict[str, Any]]:
    """Критерии приёмки с порогами из аргументов, а не с зашитыми числами.

    Не проверенный критерий получает ``checked: False`` и прочерк в отчёте: снятие
    проверки флагом не должно выглядеть как выполненная проверка. Критерий p95 при
    недостаточной выборке остаётся проверенным, но в описании прямо сказано, что
    сравнивается наихудший наблюдаемый замер, а не перцентиль. Поле ``samples`` у
    каждого критерия — размер выборки, на которой считаетось значение.
    """
    stats = latency_stats(latencies)
    recall10 = hybrid.get("recall@10", 0.0)
    hybrid_mrr = hybrid.get("mrr", 0.0)
    lexical_mrr = lexical.get("mrr", 0.0)
    criteria: dict[str, dict[str, Any]] = {
        "recall_at_10": {
            "description": f"полнота recall@10 >= {thresholds.min_recall_at_10:.2f}",
            "value": round(recall10, 3),
            "threshold": thresholds.min_recall_at_10,
            "passed": recall10 >= thresholds.min_recall_at_10,
            "checked": True,
            "samples": case_count,
        },
        "p95_latency": {
            "description": (
                f"p95 латентности ранжирования <= {thresholds.max_p95_seconds:.2f} c"
                if stats.percentile_measured
                else (
                    f"латентность ранжирования <= {thresholds.max_p95_seconds:.2f} c — "
                    f"{stats.note}"
                )
            ),
            "value": round(stats.p95, 3),
            "threshold": thresholds.max_p95_seconds,
            "passed": stats.p95 <= thresholds.max_p95_seconds,
            "checked": True,
            "samples": stats.samples,
        },
    }
    require_beats_lexical = bool(thresholds.require_hybrid_beats_lexical)
    criteria["hybrid_beats_lexical_mrr"] = {
        "description": (
            "гибрид лучше лексического базового по MRR"
            if require_beats_lexical
            else "гибрид лучше лексического базового по MRR — проверка снята флагом"
        ),
        "value": round(hybrid_mrr, 3),
        "threshold": round(lexical_mrr, 3),
        "passed": (hybrid_mrr > lexical_mrr) if require_beats_lexical else True,
        "checked": require_beats_lexical,
        "samples": case_count,
    }
    return criteria


def acceptance_passed(criteria: dict[str, dict[str, Any]]) -> bool:
    return all(item["passed"] for item in criteria.values() if item.get("checked", True))


def load_baseline(path: Path) -> dict[str, Any]:
    """Читает эталонный прогон; нечитаемый эталон — это сорванный контроль, а не 0."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"эталон {path} нечитаем: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != PAYLOAD_VERSION:
        raise ValueError(
            f"эталон {path} не подходит: ожидался прогон версии {PAYLOAD_VERSION}"
        )
    headline = data.get("headline")
    if not isinstance(headline, dict) or not all(
        isinstance(value, (int, float)) for value in headline.values()
    ):
        raise ValueError(f"в эталоне {path} нет карты headline — сравнивать нечего")
    return data


def compare_with_baseline(
    headline: dict[str, float],
    baseline: dict[str, Any],
    max_relative: float,
    current_passing: dict[str, bool],
) -> list[str]:
    """Падения относительно эталона — по форме A/B-сравнения из ``api/app.py``.

    Там ``promote`` требует одновременного неухудшения quality-метрик, потолка на
    рост латентности и явного списка кейсов ``passed_baseline - passed_candidate``.
    Здесь та же схема: метрика считается регрессией, если упала (для latency —
    выросла) сильнее ``max_relative`` от значения эталона, и отдельно перечисляются
    кейсы, которые перестали проходить. Относительно нуля падение не выразить,
    поэтому baseline == 0 пропускается.
    """
    baseline_headline = baseline["headline"]
    regressions: list[str] = []
    for key in sorted(set(headline) & set(baseline_headline)):
        base = float(baseline_headline[key])
        current = float(headline[key])
        if base <= _EPSILON:
            continue
        if key in LOWER_IS_BETTER:
            limit = base * (1 + max_relative)
            broken = current > limit
            boundary = f"допустимо не больше {limit:.3f}"
        else:
            limit = base * (1 - max_relative)
            broken = current < limit
            boundary = f"допустимо не меньше {limit:.3f}"
        if broken:
            regressions.append(f"{key}: {base:.3f} → {current:.3f} ({boundary})")
    baseline_passing = {
        str(case["query"]): bool(case["passed"]) for case in baseline.get("cases", [])
    }
    lost = {query for query, ok in baseline_passing.items() if ok} - {
        query for query, ok in current_passing.items() if ok
    }
    regressions.extend(f"кейс перестал проходить: {query[:60]}" for query in sorted(lost))
    return regressions


# ── Готовность измерения ───────────────────────────────────────────────


def preflight(manifest_path: Path, source_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Можно ли измерять вообще: манифест, каталог корпуса и полнота корпуса.

    Неполный корпус — тоже «не измеряли»: recall по половине документов нельзя
    сравнивать ни с порогом, ни с эталоном, и молча выдавать его за метрику
    качества было бы обманом.
    """
    if not manifest_path.is_file():
        return [], [f"манифест gold-кейсов не найден: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"манифест {manifest_path} нечитаем: {exc}"]
    if not isinstance(manifest, list) or not manifest:
        return [], [f"манифест {manifest_path} пуст: gold-кейсов нет"]
    if not source_root.is_dir():
        return [], [
            f"каталог корпуса не найден: {source_root} "
            "(в контейнере — /data/sources, в репозитории — «Источники информации/»)"
        ]
    missing = [
        str(item["path"]) for item in manifest if not (source_root / str(item["path"])).is_file()
    ]
    if missing:
        shown = ", ".join(missing[:3]) + (" …" if len(missing) > 3 else "")
        return manifest, [
            f"корпус неполный: отсутствует {len(missing)} из {len(manifest)} файлов — {shown}"
        ]
    return manifest, []


def parse_corpus(
    manifest: list[dict[str, Any]],
    source_root: Path,
) -> tuple[list[Finding], list[str], list[str]]:
    """Разбор документов корпуса и извлечение находок; сбои собираются списком."""
    findings: list[Finding] = []
    titles: list[str] = []
    failures: list[str] = []
    for item in manifest:
        path = source_root / str(item["path"])
        try:
            doc = parse_document(path.name, path.read_bytes(), language="ru")
            doc_findings = extract_findings_from_document(doc)
            findings.extend(doc_findings)
            titles.append(doc.title)
            print(f" Parsed: {doc.title}  ({len(doc_findings)} findings, {len(doc.text)} chars)")
        except Exception as exc:
            failures.append(f"{item['path']} — {exc}")
            print(f" FAILED: {item['path']} — {exc}")
    return findings, titles, failures


def run_retrieval_benchmark(
    findings: list[Finding],
) -> tuple[
    dict[str, float],
    dict[str, float],
    list[dict[str, Any]],
    list[float],
    list[dict[str, float]],
]:
    """Прогон hybrid против lexical baseline по всем gold-кейсам.

    Пятый элемент — метрики каждого кейса гибридного прогона: по ним видно, сколько
    значений собрала каждая агрегатная метрика (``aggregate_samples``), а не только
    «среднее по неизвестно чему».
    """
    stmt_tokens = [tokenize(f.statement) for f in findings]
    ev_tokens = [tokenize(f.evidence[0].quote) for f in findings]
    bm25_stmt = BM25Index(stmt_tokens)
    bm25_ev = BM25Index(ev_tokens)

    max_k = max(TOP_K_VALUES)
    hybrid_case_metrics: list[dict[str, float]] = []
    lexical_case_metrics: list[dict[str, float]] = []
    case_details: list[dict[str, Any]] = []
    latencies: list[float] = []

    for case in REAL_GOLD_CASES:
        expected = set(case.source_documents)

        t0 = perf_counter()
        hybrid_res = hybrid_retrieval(case.query, findings, bm25_stmt, bm25_ev, max_k)
        latencies.append(perf_counter() - t0)
        h_metrics = compute_case_metrics(hybrid_res, expected, TOP_K_VALUES)
        hybrid_case_metrics.append(h_metrics)

        lexical_res = lexical_retrieval(case.query, findings, bm25_stmt, max_k)
        l_metrics = compute_case_metrics(lexical_res, expected, TOP_K_VALUES)
        lexical_case_metrics.append(l_metrics)

        case_details.append(
            {
                "query": case.query,
                "category": case.category,
                "expected": sorted(expected),
                "hybrid_top3": [_source(f) for f in hybrid_res[:3]],
                "lexical_top3": [_source(f) for f in lexical_res[:3]],
                "hybrid_recall": h_metrics["recall@10"],
                "lexical_recall": l_metrics["recall@10"],
                "passed": bool(h_metrics["recall@10"] >= 1.0),
            }
        )
    return (
        aggregate(hybrid_case_metrics),
        aggregate(lexical_case_metrics),
        case_details,
        latencies,
        hybrid_case_metrics,
    )


def verified_answer_cases() -> int:
    """Сколько кейсов имеют ожидаемый ответ, подтверждённый источником корпуса."""
    return sum(
        1
        for case in REAL_GOLD_CASES
        if case.expected_answer and case.expected_answer_source
    )


def differential_expectation_cases() -> int:
    """Сколько кейсов имеют ожидания противоречий или пробелов.

    Это потолок измеримости ``conflict_recall``/``gap_recall``: без подтверждённых
    корпусом ожиданий дифференциаторы продукта не измеряются ни в каком прогоне,
    и отчёт обязан говорить это, а не молчать про 0.0.
    """
    return sum(
        1 for case in REAL_GOLD_CASES if case.expected_conflicts or case.expected_gaps
    )


def build_payload(
    *,
    hybrid: dict[str, float],
    lexical: dict[str, float],
    case_details: list[dict[str, Any]],
    latencies: list[float],
    doc_count: int,
    findings_count: int,
    source_root: Path,
    criteria: dict[str, dict[str, Any]],
    regressions: list[str] | None,
    baseline_path: str | None,
    max_relative: float,
    metric_samples: dict[str, int] | None = None,
    reconciliation: GoldCaseReconciliation | None = None,
) -> dict[str, Any]:
    stats = latency_stats(latencies)
    return {
        "version": PAYLOAD_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "measurement": "retrieval_benchmark",
        # Граница измерения в самой машиночитаемой форме: цифры относятся к
        # BM25/гибриду бенчмарка, а не к продуктовому retrieval и не к качеству ответа.
        "scope": (
            "локальный BM25/гибрид бенчмарка; продуктовый retrieval и качество ответа"
            " в этом прогоне не измеряются"
        ),
        "corpus": {
            "source_root": str(source_root),
            "documents": doc_count,
            "findings": findings_count,
            "gold_cases": len(case_details),
            "answer_correctness_cases": verified_answer_cases(),
            # 0 = конфликтных и пробельных ожиданий в gold-кейсах нет вообще,
            # поэтому conflict_recall/gap_recall в этом прогоне не измеримы.
            "differential_expectation_cases": differential_expectation_cases(),
        },
        "metrics": {
            "hybrid": hybrid,
            "lexical": lexical,
            "latency": {
                "average_seconds": round(stats.average, 6),
                "p95_seconds": round(stats.p95, 6),
                "samples": stats.samples,
                "p95_is_percentile": stats.percentile_measured,
                "note": stats.note,
            },
            # Среднее по какой выборке: у метрик разного набора кейсов знаменатель
            # разный, и без этого числа агрегат неотличим от «среднего по одному».
            "samples": metric_samples or {},
        },
        "headline": headline_metrics(hybrid, lexical, latencies),
        "cases": case_details,
        "acceptance": criteria,
        "gold_case_reconciliation": (
            None
            if reconciliation is None
            else {
                "balanced": reconciliation.balanced,
                "manifest_total": reconciliation.manifest_total,
                "benchmark_total": reconciliation.benchmark_total,
                "matched": reconciliation.matched,
                "diagnostics": list(reconciliation.describe()),
            }
        ),
        "regression": {
            "baseline": baseline_path,
            "max_relative": max_relative,
            "checked": regressions is not None,
            "regressions": regressions or [],
        },
    }


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")



# ── Отчёт ──────────────────────────────────────────────────────────────


def _fmt(val: float) -> str:
    return f"{val:.3f}"


def print_report(
    findings_count: int,
    doc_count: int,
    hybrid: dict[str, float],
    lexical: dict[str, float],
    case_details: list[dict[str, Any]],
    latencies: list[float],
    criteria: dict[str, dict[str, Any]],
    answer_correctness_cases: int,
    metric_samples: dict[str, int] | None = None,
    reconciliation: GoldCaseReconciliation | None = None,
) -> None:
    sep = "=" * 72
    thin = "-" * 72

    print()
    print(sep)
    print("  BENCHMARK RETRIEVAL НА РЕАЛЬНЫХ ДОКУМЕНТАХ")
    print("  Научный Клубок — Agentic GraphRAG")
    # Граница измерения: этот прогон сравнивает две функции ранжирования поверх
    # эвристического извлечения тезисов. Продуктовый гибридный поиск (Neo4j +
    # Elasticsearch + эмбеддинги GigaChat) и LLM-извлечение здесь не участвуют,
    # поэтому цифры не являются метриками качества ответа продукта.
    print("  Ранжирование: локальные BM25/гибрид бенчмарка, не продуктовый retrieval")
    print("  Точность ответа и LLM-судья в этом прогоне не измеряются (retrieval-only)")
    print(f"  Кейсов с проверенным ожидаемым ответом: {answer_correctness_cases}")
    print(
        "  Кейсов с ожиданиями противоречий/пробелов: "
        f"{differential_expectation_cases()} — при нуле conflict_recall и gap_recall"
        " не измеримы"
    )
    print(sep)
    print()
    print(f"  Корпус: {doc_count} документов, {findings_count} findings")
    print(f"  Gold-кейсы: {len(case_details)}")
    if reconciliation is not None and not reconciliation.balanced:
        # Два носителя gold-кейсов (манифест корпуса и REAL_GOLD_CASES) расходятся:
        # без этой строки разные наборы читались бы как один и тот же замер.
        print("  Сверка gold-кейсов: наборы РАЗЛИЧАЮТСЯ")
        for line in reconciliation.describe():
            print(f"    ! {line}")
    print()

    # ── Таблица метрик ──
    print(thin)
    print("  МЕТРИКИ")
    print(thin)
    print()
    samples = metric_samples or {}
    print(
        f"  {'Метрика':<20s} {'Hybrid':>10s}   {'Lexical':>10s}   {'Diff':>8s}   {'n':>4s}"
    )
    print(f"  {'─' * 20} {'─' * 10}   {'─' * 10}   {'─' * 8}   {'─' * 4}")

    metric_order = [
        "recall@3",
        "recall@5",
        "recall@10",
        "hit@3",
        "hit@10",
        "precision@3",
        "precision@5",
        "mrr",
        "ndcg@3",
    ]
    for key in metric_order:
        if key in hybrid:
            h = hybrid[key]
            low = lexical.get(key, 0.0)
            delta = h - low
            sign = "+" if delta >= 0 else ""
            # n — сколько кейсов дали значение метрики: среднее по 12 и по 3
            # выглядят одинаково, но сравнивать с порогом их нельзя одинаково.
            print(
                f"  {key:<20s} {_fmt(h):>10s}   {_fmt(low):>10s}   {sign}{delta:.3f}"
                f"   {samples.get(key, len(case_details)):>4d}"
            )
    print()

    # ── Детали по кейсам ──
    print(thin)
    print("  ДЕТАЛИ ПО КЕЙСАМ")
    print(thin)
    print()
    for i, cd in enumerate(case_details, 1):
        h_found = "✓" if cd["hybrid_recall"] >= 1.0 else "✗"
        l_found = "✓" if cd["lexical_recall"] >= 1.0 else "✗"
        print(f"  Case {i:2d} [{cd['category']}]")
        print(f"    Q: {cd['query'][:70]}...")
        print(f"    Expected: {', '.join(cd['expected'])}")
        print(
            f"    Hybrid:   recall@10={cd['hybrid_recall']:.3f} {h_found}  "
            f"top3={cd['hybrid_top3'][:3]}"
        )
        print(
            f"    Lexical:  recall@10={cd['lexical_recall']:.3f} {l_found}  "
            f"top3={cd['lexical_top3'][:3]}"
        )
        print()

    # ── Латентность ──
    print(thin)
    print("  ЛАТЕНТНОСТЬ (только hybrid-ранжирование)")
    print(thin)
    print()
    print("  Замер охватывает только hybrid_retrieval: lexical и построение индексов")
    print("  в него не входят.")
    print()
    avg_lat = latency_stats(latencies)
    print(f"  Среднее: {avg_lat.average * 1000:.1f} ms   (n={avg_lat.samples})")
    label = "p95:" if avg_lat.percentile_measured else "макс:"
    print(f"  {label}    {avg_lat.p95 * 1000:.1f} ms   (n={avg_lat.samples})")
    if not avg_lat.percentile_measured:
        print(f"  ! {avg_lat.note}")
    print()

    # ── Критерии приёмки ──
    print(thin)
    print("  КРИТЕРИИ ПРИЁМКИ")
    print(thin)
    print()
    for item in criteria.values():
        mark = "—" if not item["checked"] else ("✓" if item["passed"] else "✗")
        print(
            f"  {mark}  {item['description']}: значение {_fmt(item['value'])}, "
            f"порог {_fmt(item['threshold'])}, n={item.get('samples', 0)}"
        )
    print(f"  hit@10 (хотя бы один источник): {_fmt(hybrid.get('hit@10', 0.0))} — "
          "справочно, порога не имеет")
    print()
    print(sep)
    print()


def print_not_measured(
    problems: list[str],
    reconciliation: GoldCaseReconciliation | None = None,
) -> None:
    """Отчёт о том, что измерение не состоялось — и никаких цифр вместо него."""
    sep = "=" * 72
    print()
    print(sep)
    print("  ИЗМЕРЕНИЕ НЕ ВЫПОЛНЕНО")
    print(sep)
    print()
    for problem in problems:
        print(f"  ! {problem}")
    if reconciliation is not None and not reconciliation.balanced:
        print()
        print("  Gold-кейсы манифеста и бенчмарка различаются (сверить нужно и после")
        print("  монтирования корпуса):")
        for line in reconciliation.describe():
            print(f"    ~ {line}")
    print()
    print("  Метрики retrieval, критерии приёмки и сравнение с эталоном НЕ считались.")
    print("  Зелёного отчёта не будет: недоступный корпус — это не «качество ок».")
    print()
    print("  Что нужно: смонтировать корпус «Источники информации/» (в контейнере")
    print("  /data/sources) целиком и перезапустить прогон.")
    print()
    print(sep)
    print()


def print_gate(
    regressions: list[str] | None,
    baseline_path: Path | None,
    max_relative: float,
    accepted: bool,
) -> None:
    """Итог гейта: приёмка плюс регрессионный контроль, каждая строка — про факт."""
    sep = "=" * 72
    thin = "-" * 72
    print(thin)
    print("  РЕГРЕССИОННЫЙ КОНТРОЛЬ")
    print(thin)
    print()
    if regressions is None:
        print("  Эталон не задан (--baseline): сравнение с предыдущим прогоном не выполнялось.")
    elif not regressions:
        limit = f"{max_relative:.0%}"
        print(f"  Падений относительно эталона {baseline_path} нет (порог {limit}).")
    else:
        print(f"  Найдено падений относительно эталона {baseline_path}: {len(regressions)}")
        for regression in regressions[:20]:
            print(f"    ! {regression}")
        if len(regressions) > 20:
            print(f"    … и ещё {len(regressions) - 20}")
    print()
    print(sep)
    print(f"  ИТОГ: {'приёмка подтверждена' if accepted else 'приёмка НЕ подтверждена'}")
    print(sep)
    print()


# ── CLI ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scientific_tangle.evaluation.run_benchmark",
        description=(
            "Retrieval-бенчмарк с порогами приёмки и регрессионным контролем. "
            "Коды выхода: 0 — измерено и принято, 1 — измерено и не принято "
            "(порог или регрессия), 2 — измерение не выполнено, 3 — команда "
            "собрана неверно."
        ),
        epilog=(
            "Пороги по умолчанию соответствуют нормам продукта: recall@10 >= 0.90, "
            "p95 <= 3 c, гибрид обязан быть лучше лексического базового по MRR, "
            "падение headline-метрики относительно эталона не больше 5 %.\n"
            "Эталон пишется только на принятом прогоне: запись проваленного run'а "
            "зафиксировала бы деградацию как новую норму."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        type=Path,
        default=None,
        help="куда положить сырые метрики прогона (для архива CI)",
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        type=Path,
        default=None,
        help="JSON предыдущего прогона для проверки регрессий",
    )
    parser.add_argument(
        "--write-baseline",
        metavar="PATH",
        type=Path,
        default=None,
        help="сохранить этот прогон как эталон (только если приёмка пройдена)",
    )
    parser.add_argument(
        "--min-recall-at-10",
        type=float,
        default=MIN_RECALL_AT_10,
        help=f"порог полноты recall@10 (по умолчанию {MIN_RECALL_AT_10})",
    )
    parser.add_argument(
        "--max-p95-seconds",
        type=float,
        default=MAX_P95_SECONDS,
        help=f"порог p95 латентности ранжирования, c (по умолчанию {MAX_P95_SECONDS})",
    )
    parser.add_argument(
        "--max-regression",
        type=float,
        default=MAX_RELATIVE_REGRESSION,
        help=(
            "допустимое относительное падение headline-метрики относительно эталона "
            f"(доля, по умолчанию {MAX_RELATIVE_REGRESSION})"
        ),
    )
    parser.add_argument(
        "--require-hybrid-beats-lexical",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="требовать, чтобы гибрид был сильнее лексического базового по MRR",
    )
    parser.add_argument(
        "--source-root",
        metavar="PATH",
        type=Path,
        default=None,
        help="каталог корпуса (по умолчанию SOURCE_ROOT или «Источники информации/»)",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        type=Path,
        default=None,
        help="манифест gold-кейсов (по умолчанию preload_manifest.json пакета)",
    )
    return parser


def _configure_stdout() -> None:
    """UTF-8 в stdout: консоль Windows по умолчанию в cp866, а отчёт с кириллицей.

    mypy видит sys.stdout как абстрактный TextIO без reconfigure, поэтому
    игнорируем ошибку по месту. Под перехватчиком вывода pytest (или в CI с pipe
    без прав на перекодирование) reconfigure недоступен — это не повод валить
    прогон: вывод просто остаётся в кодировке окружения.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass


# ── Main ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as usage_exit:
        # argparse поднимает SystemExit(2) и на неверной команде, и (через код 2
        # нашего main) на отсутствующем корпусе. CI разбирает 2 как «измерение не
        # выполнено» и советует проверить /data/sources, поэтому опечатка в имени
        # флага выглядела бы как отсутствующий корпус. Причина печатается в stderr
        # самим argparse до того, как мы вернём свой код.
        return EXIT_OK if usage_exit.code in (0, None) else EXIT_USAGE
    source_root = args.source_root or SOURCE_ROOT
    manifest_path = args.manifest or MANIFEST_PATH

    # 1. Можно ли вообще измерять. Нет корпуса — нет и отчёта о метриках.
    manifest, problems = preflight(manifest_path, source_root)
    # Два носителя gold-кейсов (манифест прелоада и REAL_GOLD_CASES) сверяются,
    # как только манифест прочитан: разный набор меняет смысл recall независимо от
    # того, состоялся ли прогон.
    reconciliation = reconcile_gold_cases(manifest) if manifest else None
    if problems:
        print_not_measured(problems, reconciliation)
        return EXIT_NOT_MEASURED

    # 2. Парсинг документов и извлечение findings
    findings, doc_titles, failures = parse_corpus(manifest, source_root)
    print(f"\n Всего findings: {len(findings)} из {len(doc_titles)} документов\n")
    if not findings:
        print_not_measured(
            [
                f"не извлечено ни одной находки из {len(manifest)} документов корпуса",
                *([f"сбои разбора: {failures[0]}"[:120]] if failures else []),
            ],
            reconciliation,
        )
        return EXIT_NOT_MEASURED

    # 3. Прогон retrieval
    hybrid, lexical, case_details, latencies, hybrid_case_metrics = run_retrieval_benchmark(
        findings
    )
    metric_samples = aggregate_samples(hybrid_case_metrics)
    criteria = evaluate_acceptance(
        hybrid, lexical, latencies, args, case_count=len(case_details)
    )
    stats = latency_stats(latencies)
    print_report(
        len(findings),
        len(doc_titles),
        hybrid,
        lexical,
        case_details,
        latencies,
        criteria,
        verified_answer_cases(),
        metric_samples,
        reconciliation,
    )

    # 4. Регрессионный контроль: нечитаемый эталон срывает гейт кодом 2, а не
    #    молчаливым «падений нет».
    regressions: list[str] | None = None
    baseline_error: str | None = None
    if args.baseline is not None:
        try:
            baseline = load_baseline(args.baseline)
        except ValueError as exc:
            baseline_error = str(exc)
        else:
            regressions = compare_with_baseline(
                headline_metrics(hybrid, lexical, latencies),
                baseline,
                args.max_regression,
                passing_cases(case_details),
            )

    accepted = acceptance_passed(criteria) and not (regressions or [])
    print_gate(regressions, args.baseline, args.max_regression, accepted)

    payload = build_payload(
        hybrid=hybrid,
        lexical=lexical,
        case_details=case_details,
        latencies=latencies,
        doc_count=len(doc_titles),
        findings_count=len(findings),
        source_root=source_root,
        criteria=criteria,
        regressions=regressions,
        baseline_path=None if args.baseline is None else str(args.baseline),
        max_relative=args.max_regression,
        metric_samples=metric_samples,
        reconciliation=reconciliation,
    )
    payload["accepted"] = accepted
    if args.json_out is not None:
        write_payload(args.json_out, payload)
        tail = "p95" if stats.percentile_measured else "макс"
        print(
            f"  Метрики прогона записаны: {args.json_out} "
            f"({tail} {stats.p95:.3f} c при n={stats.samples})"
        )
    if baseline_error is not None:
        print(f"  РЕГРЕССИОННЫЙ КОНТРОЛЬ НЕ ВЫПОЛНЕН: {baseline_error}")
        print("  Retrieval измерен, но сравнивать нечем: приёмка не подтверждена.")
        return EXIT_NOT_MEASURED
    if args.write_baseline is not None:
        if accepted:
            write_payload(args.write_baseline, payload)
            print(f"  Эталон записан: {args.write_baseline}")
        else:
            print(
                f"  Эталон НЕ записан в {args.write_baseline}: прогон не прошёл приёмку, "
                "фиксировать его как норму нельзя."
            )
    return EXIT_OK if accepted else EXIT_NOT_ACCEPTED


if __name__ == "__main__":
    raise SystemExit(main())
