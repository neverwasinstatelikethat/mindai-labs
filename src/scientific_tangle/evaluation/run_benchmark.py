"""Запуск benchmark оценки retrieval качества на реальных документах.

Сравнивает hybrid retrieval (BM25 + evidence matching + entity boost)
с lexical baseline (BM25 only) на gold-кейсах из реальных документов
корпуса «Источники информации/Обзоры».
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

# Добавляем src в путь для запуска как модуля или скрипта
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scientific_tangle.domain.contracts import EvidenceLocator, Finding
from scientific_tangle.evaluation.gold_cases import REAL_GOLD_CASES
from scientific_tangle.services.document_parser import parse_document
from scientific_tangle.services.knowledge import stable_uuid

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

# ── Технические ключевые слова для фильтрации и entity boost ───────────

TECH_KEYWORDS = frozenset({
    # Элементы (русские и латинские)
    "медь", "cu", "никель", "ni", "кобальт", "co", "железо", "fe",
    "свинец", "pb", "золото", "au", "серебро", "ag", "платина", "pt",
    "палладий", "pd", "литий", "li", "цинк", "zn", "висмут",
    # Процессы
    "выщелачивание", "флотация", "обжиг", "электролиз", "электроэкстракция",
    "экстракция", "осаждение", "спекание", "конвертирование", "плавка",
    "рафинирование", "очистка", "восстановление", "окисление", "кучное",
    "хлорное", "цианидное", "автоклавное", "сульфатизирующий",
    # Материалы
    "штейн", "файнштейн", "шлак", "матт", "концентрат", "руда",
    "раствор", "электролит", "магнетит", "гематит", "ярозит", "гетит",
    "сподумен", "рассол", "сульфат", "хлорид", "цианид", "оксид",
    "оксигидроксид", "пирит", "халькопирит", "пентландит", "малахит",
    "азурит", "халькозин", "борнит", "ковеллин",
    # Оборудование
    "конвертер", "печь", "автоклав", "электропечь", "катод", "анод",
    "реактор", "фильтр", "горелка", "электрод",
    # Заводы и проекты
    "nikkelverk", "niihama", "sandouville", "panton", "albion",
    "михеевское", "томинское", "садбери", "sumitomo", "terrafame",
    "tpox", "galvanox", "cesl", "platsol", "hybinette", "хибинетт",
    "outotec", "neomet", "brixlegg", "hecla",
    # Прочее
    "мпг", "извлечение", "содержание", "температура", "давление",
    "степень", "выход", "потери", "батарей", "промпродукт",
})


# ── Стеммизация (упрощённая для русского языка) ─────────────────────

_STEM_SUFFIXES = sorted((
    "ться", "тся", "ость", "ение",
    "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями",
    "ая", "яя", "ой", "ей", "ое", "ее", "ые", "ие", "ых", "их",
    "ах", "ях", "ам", "ям", "ов", "ев", "ом", "ем", "ью", "ия",
    "ть", "шь", "ет", "ют", "ит", "ят",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь",
), key=len, reverse=True)


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

    def __init__(
        self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75
    ) -> None:
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
        "выщелачивание", "флотация", "обжиг", "электролиз",
        "электроэкстракция", "экстракция", "осаждение", "спекание",
        "штейн", "файнштейн", "шлак", "раствор", "сподумен",
        "медь", "никель", "кобальт", "железо", "литий", "свинец",
        "цианид", "магнетит", "гематит",
    ):
        if kw in s:
            return kw
    return "процесс"


def extract_findings_from_document(doc) -> list[Finding]:
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
    scores = [
        (bm25_stmt.score(q_tokens, i), i) for i in range(len(findings))
    ]
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
        q_tech_tokens = set()
        for t in q_long:
            for kw in TECH_KEYWORDS:
                if kw in t or t in kw:
                    q_tech_tokens.add(t)
                    break
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
    """Вычисляет recall@k, precision@k, MRR, NDCG@3 для одного кейса."""
    results: dict[str, float] = {}
    for k in top_k_values:
        top_k = retrieved[:k]
        rel = [1 if _source(f) in expected else 0 for f in top_k]
        # recall@k: 1 если ожидаемый источник найден в top-k, иначе 0
        found = any(r == 1 for r in rel)
        results[f"recall@{k}"] = 1.0 if found else 0.0
        if k in (3, 5):
            results[f"precision@{k}"] = sum(rel) / k
    # MRR
    first_rank = next(
        (i for i, f in enumerate(retrieved, 1) if _source(f) in expected), None
    )
    results["mrr"] = 1.0 / first_rank if first_rank else 0.0
    # NDCG@3
    rel3 = [1 if _source(f) in expected else 0 for f in retrieved[:3]]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel3))
    ideal = sum(rel3)  # кол-во релевантных в top-3
    idcg = sum(1 / math.log2(i + 2) for i in range(ideal))
    results["ndcg@3"] = dcg / idcg if idcg > 0 else 0.0
    return results


def aggregate(case_metrics: list[dict[str, float]]) -> dict[str, float]:
    if not case_metrics:
        return {}
    n = len(case_metrics)
    return {k: round(sum(m[k] for m in case_metrics) / n, 3) for k in case_metrics[0]}


# ── Отчёт ──────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    return f"{val:.3f}"


def print_report(
    findings_count: int,
    doc_count: int,
    hybrid: dict[str, float],
    lexical: dict[str, float],
    case_details: list[dict],
    latencies: list[float],
) -> None:
    sep = "=" * 72
    thin = "-" * 72

    print()
    print(sep)
    print("  BENCHMARK RETRIEVAL НА РЕАЛЬНЫХ ДОКУМЕНТАХ")
    print("  Научный Клубок — Agentic GraphRAG")
    print(sep)
    print()
    print(f"  Корпус: {doc_count} документов, {findings_count} findings")
    print(f"  Gold-кейсы: {len(case_details)}")
    print()

    # ── Таблица метрик ──
    print(thin)
    print("  МЕТРИКИ")
    print(thin)
    print()
    print(f"  {'Метрика':<20s} {'Hybrid':>10s}   {'Lexical':>10s}   {'Diff':>8s}")
    print(f"  {'─'*20} {'─'*10}   {'─'*10}   {'─'*8}")

    metric_order = [
        "recall@3", "recall@5", "recall@10",
        "precision@3", "precision@5",
        "mrr", "ndcg@3",
    ]
    for key in metric_order:
        if key in hybrid:
            h = hybrid[key]
            l = lexical.get(key, 0.0)
            delta = h - l
            sign = "+" if delta >= 0 else ""
            print(
                f"  {key:<20s} {_fmt(h):>10s}   {_fmt(l):>10s}   "
                f"{sign}{delta:.3f}"
            )
    print()

    # ─<arg_value> Детали по кейсам ──
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
    print("  ЛАТЕНТНОСТЬ")
    print(thin)
    print()
    avg_lat = sum(latencies) / max(len(latencies), 1)
    sorted_lat = sorted(latencies)
    p95_idx = int(len(sorted_lat) * 0.95)
    p95 = sorted_lat[p95_idx] if sorted_lat else 0.0
    print(f"  Среднее: {avg_lat * 1000:.1f} ms")
    print(f"  p95:     {p95 * 1000:.1f} ms")
    print()

    # ── Критерии приёмки ──
    print(thin)
    print("  КРИТЕРИИ ПРИЁМКИ")
    print(thin)
    print()
    recall10 = hybrid.get("recall@10", 0.0)
    recall_pass = recall10 >= 0.90
    latency_pass = p95 <= 3.0
    hybrid_better = hybrid.get("mrr", 0) > lexical.get("mrr", 0)
    print(
        f"  recall@10 >= 0.90:  {'✓' if recall_pass else '✗'} "
        f"({recall10:.3f})"
    )
    print(
        f"  p95 latency <= 3s:  {'✓' if latency_pass else '✗'} "
        f"({p95:.3f}s)"
    )
    print(
        f"  Hybrid > Lexical:  {'✓' if hybrid_better else '✗'} "
        f"(MRR: {_fmt(hybrid.get('mrr', 0))} vs {_fmt(lexical.get('mrr', 0))})"
    )
    print()
    print(sep)
    print()


# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    # Кодировка stdout в UTF-8 для корректного вывода Unicode
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    # 1. Загрузка манифеста
    manifest = json.loads(MANIFEST_PATH.read_text("utf-8"))

    # 2. Парсинг документов и извлечение findings
    all_findings: list[Finding] = []
    doc_titles: list[str] = []
    for item in manifest:
        path = SOURCE_ROOT / item["path"]
        try:
            content = path.read_bytes()
            doc = parse_document(path.name, content, language="ru")
            findings = extract_findings_from_document(doc)
            all_findings.extend(findings)
            doc_titles.append(doc.title)
            print(
                f" Parsed: {doc.title}  "
                f"({len(findings)} findings, {len(doc.text)} chars)"
            )
        except Exception as exc:
            print(f" FAILED: {item['path']} — {exc}")

    print(f"\n Всего findings: {len(all_findings)} из {len(doc_titles)} документов\n")

    if not all_findings:
        print(" Нет findings для benchmark. Выход.")
        return

    # 3. Построение BM25 индексов
    stmt_tokens = [tokenize(f.statement) for f in all_findings]
    ev_tokens = [tokenize(f.evidence[0].quote) for f in all_findings]
    bm25_stmt = BM25Index(stmt_tokens)
    bm25_ev = BM25Index(ev_tokens)

    # 4. Запуск benchmark
    max_k = max(TOP_K_VALUES)
    hybrid_case_metrics: list[dict[str, float]] = []
    lexical_case_metrics: list[dict[str, float]] = []
    case_details: list[dict] = []
    latencies: list[float] = []

    for case in REAL_GOLD_CASES:
        expected = set(case.source_documents)

        # Hybrid
        t0 = perf_counter()
        hybrid_res = hybrid_retrieval(
            case.query, all_findings, bm25_stmt, bm25_ev, max_k
        )
        latencies.append(perf_counter() - t0)
        h_metrics = compute_case_metrics(hybrid_res, expected, TOP_K_VALUES)
        hybrid_case_metrics.append(h_metrics)

        # Lexical
        lexical_res = lexical_retrieval(
            case.query, all_findings, bm25_stmt, max_k
        )
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
            }
        )

    # 5. Агрегация и отчёт
    hybrid_agg = aggregate(hybrid_case_metrics)
    lexical_agg = aggregate(lexical_case_metrics)
    print_report(
        len(all_findings),
        len(doc_titles),
        hybrid_agg,
        lexical_agg,
        case_details,
        latencies,
    )


if __name__ == "__main__":
    main()
