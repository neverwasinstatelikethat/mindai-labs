from __future__ import annotations

import base64
import json

import fitz

from scientific_tangle.domain.contracts import AnswerPayload


class ExportService:
    """Форматирует AnswerPayload в Markdown и JSON-LD."""

    def export(self, answer: AnswerPayload, fmt: str) -> tuple[str, str, str]:
        """Возвращает (content, content_type, filename)."""
        if fmt == "json-ld":
            return self._to_json_ld(answer)
        if fmt == "pdf":
            return self._to_pdf(answer)
        return self._to_markdown(answer)

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
                    lines.append(
                        f"- **Источник:** {ev.source_title}, стр. {ev.page}"
                    )
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

        lines.extend([
            "## Метаданные",
            f"- **Режим модели:** {answer.model_mode}",
            f"- **Общая уверенность:** {answer.confidence}",
            f"- **Trace событий:** {len(answer.trace)}",
            "",
        ])

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
        """Генерирует PDF из AnswerPayload через PyMuPDF."""
        # Строим текстовые блоки аналогично markdown-экспорту
        blocks: list[str] = [
            f"Ответ на вопрос: {answer.question}",
            "",
            f"Краткое содержание\n{answer.summary}",
        ]

        if answer.findings:
            parts: list[str] = ["Утверждения"]
            for index, finding in enumerate(answer.findings, start=1):
                parts.append(f"{index}. {finding.statement}")
                parts.append(f"   Уверенность: {finding.confidence}")
                parts.append(f"   Статус: {finding.status}")
                for ev in finding.evidence:
                    parts.append(
                        f"   Источник: {ev.source_title}, стр. {ev.page}"
                    )
                    parts.append(f"   > {ev.quote}")
                if finding.observations:
                    obs_text = "; ".join(
                        f"{o.property_name}={o.raw_text}"
                        for o in finding.observations
                    )
                    parts.append(f"   Числовые данные: {obs_text}")
            blocks.append("\n".join(parts))

        if answer.conflicts:
            blocks.append(
                "Конфликты\n" + "\n".join(f"- {item}" for item in answer.conflicts)
            )

        if answer.knowledge_gaps:
            blocks.append(
                "Пробелы в знаниях\n"
                + "\n".join(f"- {item}" for item in answer.knowledge_gaps)
            )

        if answer.recommendations:
            blocks.append(
                "Рекомендации\n"
                + "\n".join(f"- {item}" for item in answer.recommendations)
            )

        blocks.append(
            "Метаданные\n"
            f"Режим модели: {answer.model_mode}\n"
            f"Общая уверенность: {answer.confidence}\n"
            f"Trace событий: {len(answer.trace)}"
        )

        text = "\n\n".join(blocks)

        # Разбиваем на страницы по границам строк
        lines = text.split("\n")
        pages: list[str] = []
        current: list[str] = []
        current_len = 0
        limit = 3500  # ~3500 символов на A4 при fontsize=10
        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > limit and current:
                pages.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len
        if current:
            pages.append("\n".join(current))

        doc = fitz.open()
        for page_text in pages:
            page = doc.new_page()
            rect = fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50)
            page.insert_textbox(rect, page_text, fontsize=10, fontname="helv")

        pdf_bytes = doc.tobytes()
        doc.close()
        content = base64.b64encode(pdf_bytes).decode("ascii")
        return content, "application/pdf", "answer.pdf"
