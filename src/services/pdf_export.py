"""PDF export for personalized Demo Day recommendations."""

import io
from pathlib import Path

from fpdf import FPDF

from src.models.project import Project
from src.models.recommendation import Recommendation

FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "fonts"


async def generate_recommendations_pdf(
    recs: list[Recommendation],
    projects: list[Project],
    user_name: str = "Участник",
    event_name: str = "Demo Day",
) -> io.BytesIO:
    """Build a PDF with the ranked recommendation list.

    Args:
        recs: sorted recommendations.
        projects: pre-loaded projects matching recs.
        user_name: display name for the header.
        event_name: event title.

    Returns:
        BytesIO buffer with the PDF content.
    """
    pdf = FPDF()
    pdf.add_font("DejaVu", "", str(FONTS_DIR / "DejaVuSans.ttf"), uni=True)
    pdf.add_font("DejaVu", "B", str(FONTS_DIR / "DejaVuSans-Bold.ttf"), uni=True)
    pdf.add_page()

    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 10, f"Программа {event_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    pdf.cell(0, 8, f"Подготовлено для: {user_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    projects_by_id = {p.id: p for p in projects}

    for rec in recs:
        project = projects_by_id.get(rec.project_id)
        if not project:
            continue

        pdf.set_font("DejaVu", "B", 12)
        pdf.cell(0, 8, f"#{rec.rank} {project.title}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 10)

        if project.description:
            pdf.multi_cell(0, 6, project.description[:300], new_x="LMARGIN", new_y="NEXT")
        meta_lines = []
        if project.tags:
            meta_lines.append(f"Теги: {', '.join(str(t) for t in project.tags)}")
        if project.tech_stack:
            meta_lines.append(f"Стек: {', '.join(str(t) for t in project.tech_stack)}")
        if project.author:
            meta_lines.append(f"Автор: {project.author}")
        if project.telegram_contact:
            meta_lines.append(f"Контакт: {project.telegram_contact}")
        if meta_lines:
            pdf.set_font("DejaVu", "", 9)
            pdf.multi_cell(0, 5, "\n".join(meta_lines), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("DejaVu", "", 10)
        pdf.ln(3)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf
