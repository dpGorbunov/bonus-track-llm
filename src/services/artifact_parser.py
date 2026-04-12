"""Artifact parser service: PPTX, PDF, GitHub README extraction + LLM structured extraction.

Parses project artifacts (presentations, PDFs, GitHub READMEs) and uses LLM
to extract structured ProjectExtraction data from raw text.
"""

import io
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)


async def parse_pptx(url_or_path: str) -> str:
    """Extract text from PPTX file. Handles URLs and local paths."""
    from pptx import Presentation

    if url_or_path.startswith("http"):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url_or_path)
            resp.raise_for_status()
            data = io.BytesIO(resp.content)
    else:
        data = url_or_path

    prs = Presentation(data)
    slides_text: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
        if texts:
            slides_text.append(f"Слайд {i}: {' | '.join(texts)}")

    return "\n".join(slides_text)


async def parse_pdf(url_or_path: str) -> str:
    """Extract text from PDF file."""
    import pymupdf

    if url_or_path.startswith("http"):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url_or_path)
            resp.raise_for_status()
            data = resp.content
    else:
        with open(url_or_path, "rb") as f:
            data = f.read()

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages_text: list[str] = []
    for i, page in enumerate(doc, 1):
        text = page.get_text().strip()
        if text:
            pages_text.append(f"Страница {i}: {text[:500]}")
    doc.close()

    return "\n".join(pages_text)


async def parse_github_readme(github_url: str) -> str:
    """Fetch README from GitHub repo."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)", github_url)
    if not match:
        return ""
    owner, repo = match.group(1), match.group(2).rstrip(".git")

    async with httpx.AsyncClient(timeout=15) as client:
        for branch in ["main", "master"]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text[:3000]
    return ""


async def parse_presentation(url: str) -> str:
    """Auto-detect format (PPTX or PDF) and parse."""
    url_lower = url.lower()
    if url_lower.endswith(".pptx"):
        return await parse_pptx(url)
    elif url_lower.endswith(".pdf"):
        return await parse_pdf(url)
    else:
        # Try PDF first (more common), fallback to PPTX
        try:
            return await parse_pdf(url)
        except Exception:
            try:
                return await parse_pptx(url)
            except Exception:
                logger.warning("Could not parse presentation: %s", url)
                return ""


async def extract_structured(
    raw_text: str,
    project_title: str,
    project_description: str,
    platform_client,
) -> dict:
    """Use LLM to extract structured ProjectExtraction from raw text."""
    from src.schemas.tools import ProjectExtraction

    prompt = (
        f"Проект: {project_title}\n"
        f"Описание: {project_description}\n\n"
        f"Текст из артефактов (презентация/README):\n{raw_text[:5000]}\n\n"
        "Извлеки структурированную информацию. "
        "ПРАВИЛА:\n"
        "- Извлекай ТОЛЬКО то, что ЯВНО указано в тексте\n"
        "- Если данных нет - ставь null\n"
        "- key_metrics: конкретные числа (accuracy, F1, latency, MAU)\n"
        "- red_flags: несоответствия, нереалистичные заявления. "
        "Каждый flag: {category, description, severity}\n"
        "- production_readiness: prototype | mvp | production\n"
        "- stack: технологии из текста (не из описания проекта)\n"
        "Ответь строго JSON."
    )

    system = (
        "Ты аналитик студенческих AI-проектов. "
        "Извлекай факты из текста презентаций и README. "
        "НЕ выдумывай данные. Если информации нет - null."
    )

    try:
        resp = await platform_client.chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = resp["choices"][0]["message"]["content"]
        data = json.loads(content)
        extraction = ProjectExtraction(**data)
        return extraction.model_dump()
    except Exception as e:
        logger.error("Structured extraction failed for %s: %s", project_title, e)
        return {}
