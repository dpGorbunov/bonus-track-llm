#!/usr/bin/env python3
"""Batch GitHub analysis for all projects with github_url.

Usage:
    python -m scripts.analyze_github           # analyze only new projects
    python -m scripts.analyze_github --force   # re-analyze all projects
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("BOT_TOKEN", "analyze")

from sqlalchemy import select

from src.core.config import settings
from src.core.database import async_session
from src.models.project import Project
from src.services.github_analyzer import analyze_repo, parse_github_url

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    force = "--force" in sys.argv
    token = os.environ.get("GITHUB_TOKEN", settings.github_token)

    async with async_session() as db:
        result = await db.execute(
            select(Project).where(Project.github_url.isnot(None))
        )
        projects = result.scalars().all()

        logger.info("Found %d projects with GitHub URLs", len(projects))

        analyzed = 0
        for project in projects:
            pc = project.parsed_content or {}

            # Skip if already analyzed (unless --force)
            if not force and pc.get("github", {}).get("analyzed_at"):
                logger.info("  Skip %s (already analyzed)", project.title)
                continue

            parsed = parse_github_url(project.github_url)
            if not parsed:
                logger.warning(
                    "  Invalid URL for %s: %s", project.title, project.github_url
                )
                continue

            owner, repo = parsed
            logger.info("  Analyzing %s (%s/%s)...", project.title, owner, repo)

            github_data = await analyze_repo(owner, repo, token)

            pc["github"] = github_data
            project.parsed_content = pc
            await db.flush()
            analyzed += 1

            # Rate limit: pause between repos
            await asyncio.sleep(1)

        await db.commit()
        logger.info("Done. Analyzed %d repos.", analyzed)


if __name__ == "__main__":
    asyncio.run(main())
