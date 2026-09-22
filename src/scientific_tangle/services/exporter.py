from __future__ import annotations

import base64
import json

import fitz

from scientific_tangle.domain.contracts import AnswerPayload, ExportFormat

# Base-14 («helv») кириллицы не содержит: в прежнем PDF русские слова
# вырезались молча, и файл выглядел как пустая сетка вопросительных знаков.
# MuPDF везёт в колесе «Droid Sans Fallback» — единственный шрифт с кириллическими
# глифами, доступный без новых зависимостей и без файлов в образе.
PDF_FONT_NAME = "cjk"
PDF_FONT_ALIAS = "nk-sans"
PDF_PAGE_SIZE = (595, 842)  # A4 в пунктах
PDF_MARGIN = 50
PDF_FONT_SIZE = 10.5
PDF_LINE_HEIGHT = 1.45
PDF_FOOTPRINT_SAMPLE = "Жщ"

_pdf_font: fitz.Font | None = None


class ExportError(RuntimeError):
    """Файл не собран: молча отдавать PDF без кириллических глифов нельзя."""


def _font() -> fitz.Font:
    """Шрифт с кириллицей, загружаемый один раз на процесс (буфер ~3.5 МБ)."""
    global _pdf_font
    if _pdf_font is None:
        try:
            font = fitz.Font(PDF_FONT_NAME)
        except Exception as error:  # noqa: BLE001 — причина уйдёт клиенту текстом
            raise ExportError(f"PDF-шрифт недоступен: {error}") from error
        missing = [char for char in PDF_FOOTPRINT_SAMPLE if not font.has_glyph(ord(char))]
        if missing:
            raise ExportError(f"Шрифт {font.name} не покрывает кириллицу")
        _pdf_font = font
    return _pdf_font


class ExportService:
    """Форматирует AnswerPayload в Markdown, JSON-LD и PDF."""

    def export(self, answer: AnswerPayload, fmt: ExportFormat) -> tuple[str, str, str]:
        """Возвращает (content, content_type, filename).

        Неизвестный формат — ошибка, а не молчаливая подмена Markdown: клиент,
        попросивший «docx» и получивший .md, потом лечит это как потерю данных.
        """
        if fmt == "json-ld":
            return self._to_json_ld(answer)
        if fmt == "pdf":
            return self._to_pdf(answer)
        if fmt == "markdown":
            return self._to_markdown(answer)
        raise ExportError(f"Формат выгрузки не поддерживается: {fmt}")

    @staticmethod
    def _to_markdown(answer: AnswerPayload) -> tuple[str, str, str]:
        lines: list[str] = [
            f"# Ответ на вопрос: {answer.question}",
            "",
            "## Краткое содержание",
            answer.summary,
            "",
        ]

        if answer.findings:
            lines.append("## Утверждения")
            for index, finding in enumerate(answer.findings, start=1):
                lines.append(f"### {index}. {finding.statement}")
                lines.append(f"- **Уверенность:** {finding.confidence}")
                lines.append(f"- **Статус:** {finding.status}")
                for ev in finding.evidence:
                    lines.append(f"- **Источник:** {ev.source_title}, стр. {ev.page}")
                    lines.append(f"  > {ev.quote}")
                if finding.observations:
                    obs_text = "; ".join(
                        f"{o.property_name}={o.raw_text}" for o in finding.observations
                    )
                    lines.append(f"- **Числовые данные:** {obs_text}")
                lines.append("")

        if answer.conflicts:
            lines.append("## Конфликты")
            for item in answer.conflicts:
                lines.append(f"- {item}")
            lines.append("")

        if answer.knowledge_gaps:
            lines.append("## Пробелы в знаниях")
            for item in answer.knowledge_gaps:
                lines.append(f"- {item}")
            lines.append("")

        if answer.recommendations:
            lines.append("## Рекомендации")
            for item in answer.recommendations:
                lines.append(f"- {item}")
            lines.append("")

        lines.extend(
            [
                "## Метаданные",
                f"- **Режим модели:** {answer.model_mode}",
                f"- **Общая уверенность:** {answer.confidence}",
                f"- **Trace событий:** {len(answer.trace)}",
                "",
            ]
        )

        content = "\n".join(lines)
        return content, "text/markdown; charset=utf-8", "answer.md"

    @staticmethod
    def _to_json_ld(answer: AnswerPayload) -> tuple[str, str, str]:
        context = {
            "schema": "https://schema.org/",
            "claim": "https://ontology.mindai.ru/claim/",
            "evidence": "https://ontology.mindai.ru/evidence/",
        }
        findings_ld = [
            {
                "@type": "claim:Finding",
                "claim:id": f.id,
                "claim:statement": f.statement,
                "claim:confidence": f.confidence,
                "claim:status": f.status,
                "claim:version": f.version,
                "evidence:sources": [
                    {
                        "evidence:source_title": ev.source_title,
                        "evidence:page": ev.page,
                        "evidence:quote": ev.quote,
                    }
                    for ev in f.evidence
                ],
                "claim:observations": [
                    {
                        "property": o.property_name,
                        "value": o.raw_text,
                        "unit": o.normalized_unit,
                    }
                    for o in f.observations
                ],
            }
            for f in answer.findings
        ]
        document = {
            "@context": context,
            "@type": "schema:ScholarlyArticle",
            "schema:question": answer.question,
            "schema:abstract": answer.summary,
            "schema:confidence": answer.confidence,
            "schema:modelMode": answer.model_mode,
            "claim:findings": findings_ld,
            "claim:conflicts": answer.conflicts,
            "claim:knowledgeGaps": answer.knowledge_gaps,
            "claim:recommendations": answer.recommendations,
        }
        content = json.dumps(document, ensure_ascii=False, indent=2)
        return content, "application/ld+json; charset=utf-8", "answer.jsonld"

    @staticmethod
    def _to_pdf(answer: AnswerPayload) -> tuple[str, str, str]:
        """Собирает PDF через PyMuPDF шрифтом, в котором кириллица есть.

        Три вещи, без которых «PDF» был бы обманкой: шрифт с кириллическими
        глифами, разбивка по фактической ширине строк (иначе хвост обрезаился за
        полем страницы) и проверка собранного файла перед выдачей.
        """
        font = _font()
        text = "\n\n".join(_pdf_blocks(answer))
        width = PDF_PAGE_SIZE[0] - 2 * PDF_MARGIN
        height = PDF_PAGE_SIZE[1] - 2 * PDF_MARGIN
        lines_per_page = max(int(height / (PDF_FONT_SIZE * PDF_LINE_HEIGHT)), 1)
        pages = _paginate(text, font, width, lines_per_page)
        rect = fitz.Rect(PDF_MARGIN, PDF_MARGIN, PDF_PAGE_SIZE[0] - PDF_MARGIN,
                         PDF_PAGE_SIZE[1] - PDF_MARGIN)

        doc = fitz.open()
        try:
            for page_text in pages:
                page = doc.new_page(width=PDF_PAGE_SIZE[0], height=PDF_PAGE_SIZE[1])
                page.insert_font(fontname=PDF_FONT_ALIAS, fontbuffer=font.buffer)
                # insert_textbox возвращает <0, когда текст не влез: молча
                # выбросить часть ответа здесь хуже ошибки.
                fitted = page.insert_textbox(
                    rect,
                    page_text,
                    fontsize=PDF_FONT_SIZE,
                    fontname=PDF_FONT_ALIAS,
                    lineheight=PDF_LINE_HEIGHT,
                )
                if fitted < 0:
                    raise ExportError("Страница экспорта не вместилась в формат A4")
            doc.subset_fonts()  # без субзетинга файл ответа весит мегабайты
            payload = doc.tobytes(deflate=True)
        finally:
            doc.close()

        _verify_pdf(payload, len(pages), answer)
        return base64.b64encode(payload).decode("ascii"), "application/pdf", "answer.pdf"


def _pdf_blocks(answer: AnswerPayload) -> list[str]:
    """Текстовые блоки PDF — те же разделы, что и в markdown-экспорте."""
    blocks: list[str] = [
        f"Ответ на вопрос: {answer.question}",
        f"Краткое содержание\n{answer.summary}",
    ]
    if answer.findings:
        parts: list[str] = ["Утверждения"]
        for index, finding in enumerate(answer.findings, start=1):
            parts.append(f"{index}. {finding.statement}")
            parts.append(f"   Уверенность: {finding.confidence}")
            parts.append(f"   Статус: {finding.status}")
            for ev in finding.evidence:
                parts.append(f"   Источник: {ev.source_title}, стр. {ev.page}")
                parts.append(f"   > {ev.quote}")
            if finding.observations:
                obs_text = "; ".join(
                    f"{o.property_name}={o.raw_text}" for o in finding.observations
                )
                parts.append(f"   Числовые данные: {obs_text}")
        blocks.append("\n".join(parts))
    if answer.conflicts:
        blocks.append("Конфликты\n" + "\n".join(f"- {item}" for item in answer.conflicts))
    if answer.knowledge_gaps:
        blocks.append(
            "Пробелы в знаниях\n" + "\n".join(f"- {item}" for item in answer.knowledge_gaps)
        )
    if answer.recommendations:
        blocks.append(
            "Рекомендации\n" + "\n".join(f"- {item}" for item in answer.recommendations)
        )
    blocks.append(
        "Метаданные\n"
        f"Режим модели: {answer.model_mode}\n"
        f"Общая уверенность: {answer.confidence}\n"
        f"Trace событий: {len(answer.trace)}"
    )
    return blocks


def _paginate(text: str, font: fitz.Font, width: float, lines_per_page: int) -> list[str]:
    """Переносит строки по фактической ширине глифов и режет на страницы по высоте.

    Прежний перенос «по 3500 символам» не учитывал ни ширину, ни высоту: длинная
    цитата уходила за поле страницы и исчезала из файла без всякого сигнала.
    """
    wrapped: list[str] = []
    for line in text.split("\n"):
        if not line:
            wrapped.append("")
            continue
        words = line.split(" ")
        current = ""
        indent = " " * (len(line) - len(line.lstrip()))
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            if current and font.text_length(candidate, fontsize=PDF_FONT_SIZE) > width:
                wrapped.append(current)
                current = f"{indent}{word}"
            else:
                current = candidate
            while font.text_length(current, fontsize=PDF_FONT_SIZE) > width:
                # Одно слово шире полосы (длинный URL или цитата без пробелов).
                cut = max(len(current) // 2, 1)
                while cut > 1 and font.text_length(
                    current[:cut], fontsize=PDF_FONT_SIZE
                ) > width:
                    cut //= 2
                wrapped.append(current[:cut])
                current = current[cut:]
        wrapped.append(current)
    return [
        "\n".join(wrapped[start : start + lines_per_page])
        for start in range(0, len(wrapped), lines_per_page)
    ]


def _verify_pdf(payload: bytes, pages: int, answer: AnswerPayload) -> None:
    """Проверяет возвращаемый файл, а не только то, что генератор не упал.

    Контроль дешёвый и злой: перечитывает собранный PDF и сверяет кириллический
    фрагмент ответа. Так «PDF есть, а текста в нём нет» перестаёт быть
    возможным исходом — именно так выглядел экспорт до шрифта с кириллицей.
    """
    probe_source = "".join(answer.question.split()) or "".join(answer.summary.split())
    probe = probe_source[:24]
    if len(probe) < 3:
        raise ExportError("Слишком короткий ответ, чтобы проверить выгрузку в PDF")
    try:
        checked = fitz.open(stream=payload, filetype="pdf")
    except Exception as error:  # noqa: BLE001 — файл не собран, это отказ экспорта
        raise ExportError(f"PDF не читается после сборки: {error}") from error
    try:
        if checked.page_count != pages:
            raise ExportError(f"PDF содержит {checked.page_count} страниц вместо {pages}")
        extracted = "".join("".join(page.get_text().split()) for page in checked)
    finally:
        checked.close()
    if probe not in extracted:
        raise ExportError("В PDF нет кириллического текста: шрифт не покрывает ответ")

