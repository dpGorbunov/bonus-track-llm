#!/usr/bin/env python3
"""Batch parse artifacts for all projects."""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("BOT_TOKEN", "parse")

from pydantic import SecretStr
from sqlalchemy import select

from src.core.config import settings
from src.core.database import async_session
from src.models.project import Project
from src.services.artifact_parser import (
    extract_structured,
    parse_github_readme,
    parse_presentation,
)
from src.services.platform_client import PlatformClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    platform = PlatformClient("https://openrouter.ai/api", "unused")
    api_key = os.environ.get("OPENROUTER_API_KEY", settings.openrouter_api_key)
    if not api_key:
        logger.error("OPENROUTER_API_KEY required")
        return
    platform._token = SecretStr(api_key)

    async with async_session() as db:
        result = await db.execute(
            select(Project).where(Project.parsed_content.is_(None))
        )
        projects = result.scalars().all()

        if not projects:
            logger.info("All projects already parsed")
            return

        logger.info("Parsing %d projects...", len(projects))

        for project in projects:
            raw_parts: list[str] = []

            # Parse presentation
            if project.presentation_url:
                try:
                    text = await parse_presentation(project.presentation_url)
                    if text:
                        raw_parts.append(text)
                        logger.info("  Parsed presentation: %s", project.title)
                except Exception as e:
                    logger.warning(
                        "  Presentation parse failed for %s: %s",
                        project.title,
                        e,
                    )

            # Parse GitHub
            if project.github_url:
                try:
                    readme = await parse_github_readme(project.github_url)
                    if readme:
                        raw_parts.append(f"GitHub README:\n{readme}")
                        logger.info("  Parsed GitHub: %s", project.title)
                except Exception as e:
                    logger.warning(
                        "  GitHub parse failed for %s: %s",
                        project.title,
                        e,
                    )

            # If no artifacts, use description only
            raw_text = "\n\n".join(raw_parts) if raw_parts else project.description

            # LLM extraction
            extraction = await extract_structured(
                raw_text, project.title, project.description, platform
            )

            if extraction:
                project.parsed_content = extraction
                await db.flush()
                logger.info("  Extracted: %s", project.title)
            else:
                logger.warning("  Extraction failed: %s", project.title)

        await db.commit()
        logger.info("Done. %d projects processed.", len(projects))

    await platform.close()


if __name__ == "__main__":
    asyncio.run(main())
