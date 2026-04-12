"""EventAI Agent - main entry point.

Initializes:
- Redis connection (FSM storage + throttle)
- PlatformClient (LLM proxy) with registration
- aiogram Bot + Dispatcher
- FSM storage (Redis-backed)
- All middlewares (DB session, platform, throttle, reconcile)
- All routers (start, profiling, program, detail, support, expert)
- Health check endpoint (aiohttp on port 8080)
- Polling loop
- Graceful shutdown on SIGTERM
"""

import asyncio
import logging
import signal

import redis.asyncio as aioredis
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage

from src.bot.middlewares import (
    DbSessionMiddleware,
    PlatformMiddleware,
    ReconcileMiddleware,
    ThrottleMiddleware,
)
from src.bot.routers import (
    detail_router,
    expert_router,
    fallback_router,
    profiling_router,
    program_router,
    start_router,
    support_group_router,
    support_router,
)
from src.core.config import settings
from src.services.platform_client import PlatformClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint for container orchestration."""
    return web.Response(text="ok")


async def run_health_server() -> web.AppRunner:
    """Start aiohttp health check server on port 8080."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Health endpoint listening on :8080/health")
    return runner


async def _auto_seed() -> None:
    """Seed demo data if database is empty (first run)."""
    from sqlalchemy import select, text
    from src.core.database import async_session
    from src.models.event import Event

    async with async_session() as db:
        result = await db.execute(select(Event).limit(1))
        has_data = result.scalar_one_or_none() is not None

        if has_data:
            logger.info("Database has data, skipping seed")
        else:
            logger.info("Empty database - seeding demo data...")
            from pathlib import Path

            seed_file = Path(__file__).parent.parent / "scripts" / "demo_seed.sql"
            if seed_file.exists():
                sql = seed_file.read_text()
                for statement in sql.split(";"):
                    stmt = statement.strip()
                    if stmt and not stmt.startswith("--"):
                        try:
                            await db.execute(text(stmt))
                        except Exception as e:
                            logger.warning("Seed SQL error (continuing): %s", e)
                await db.commit()
                logger.info("Demo data seeded")

        # Always check and embed projects if needed
        if settings.openrouter_api_key:
            try:
                await _embed_demo_projects(db)
            except Exception as e:
                logger.warning("Auto-embedding failed (tag overlap will be used): %s", e)

            try:
                await _auto_parse_artifacts(db)
            except Exception as e:
                logger.warning("Auto artifact parsing failed: %s", e)


async def _embed_demo_projects(db) -> None:
    """Embed demo projects via OpenRouter for pgvector search."""
    import httpx
    from sqlalchemy import select, text as sql_text
    from src.models.project import Project

    result = await db.execute(select(Project).where(Project.embedding.is_(None)))
    projects = result.scalars().all()

    if not projects:
        return

    logger.info("Embedding %d projects...", len(projects))

    async with httpx.AsyncClient(timeout=30.0) as client:
        for project in projects:
            tags = ", ".join(project.tags or [])
            stack = ", ".join(project.tech_stack or [])
            embed_text = f"{project.title}. {project.description}. Теги: {tags}. Стек: {stack}"

            resp = await client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={"model": settings.embedding_model, "input": embed_text},
            )
            if resp.status_code != 200:
                logger.warning("Embedding failed for %s: %s", project.title, resp.status_code)
                continue

            embedding = resp.json()["data"][0]["embedding"]
            emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
            await db.execute(
                sql_text("UPDATE projects SET embedding = cast(:emb as vector) WHERE id = cast(:pid as uuid)"),
                {"emb": emb_str, "pid": str(project.id)},
            )
            logger.info("  Embedded: %s (dim=%d)", project.title, len(embedding))

    await db.commit()
    logger.info("All projects embedded")


async def _auto_parse_artifacts(db) -> None:
    """Extract structured data from artifacts for projects with NULL parsed_content."""
    from sqlalchemy import select
    from src.models.project import Project
    from src.services.artifact_parser import (
        extract_structured,
        parse_github_readme,
        parse_presentation,
    )
    from pydantic import SecretStr

    result = await db.execute(
        select(Project).where(Project.parsed_content.is_(None))
    )
    projects = result.scalars().all()

    if not projects:
        return

    logger.info("Parsing artifacts for %d projects...", len(projects))

    platform = PlatformClient("https://openrouter.ai/api", "unused")
    platform._token = SecretStr(settings.openrouter_api_key)

    try:
        for project in projects:
            raw_parts: list[str] = []

            if project.presentation_url:
                try:
                    text = await parse_presentation(project.presentation_url)
                    if text:
                        raw_parts.append(text)
                except Exception as e:
                    logger.warning("Presentation parse failed for %s: %s", project.title, e)

            if project.github_url:
                try:
                    readme = await parse_github_readme(project.github_url)
                    if readme:
                        raw_parts.append(f"GitHub README:\n{readme}")
                except Exception as e:
                    logger.warning("GitHub parse failed for %s: %s", project.title, e)

            raw_text = "\n\n".join(raw_parts) if raw_parts else project.description

            extraction = await extract_structured(
                raw_text, project.title, project.description, platform
            )

            if extraction:
                project.parsed_content = extraction
                await db.flush()
                logger.info("  Parsed artifact: %s", project.title)

        await db.commit()
        logger.info("Artifact parsing complete")
    finally:
        await platform.close()


async def main() -> None:
    logger.info("EventAI Agent starting...")

    # --- Redis ---
    redis_kwargs = {"decode_responses": True}
    if settings.redis_password:
        redis_kwargs["password"] = settings.redis_password
    redis = aioredis.from_url(settings.redis_url, **redis_kwargs)
    try:
        await redis.ping()
        logger.info("Redis connected: %s", settings.redis_url)
    except Exception as e:
        logger.error("Redis connection failed: %s", e)
        raise

    # --- Platform Client ---
    # Standalone mode: use OpenRouter directly (no llm-agent-platform)
    # Platform mode: register with llm-agent-platform proxy
    if settings.openrouter_api_key and not settings.master_token:
        from pydantic import SecretStr
        platform = PlatformClient(
            platform_url="https://openrouter.ai/api",
            master_token="unused",
        )
        platform._token = SecretStr(settings.openrouter_api_key)
        logger.info("Standalone mode: OpenRouter direct (%s)", settings.llm_model)
    else:
        platform = PlatformClient(
            platform_url=settings.platform_url,
            master_token=settings.master_token,
        )
        try:
            await platform.register()
            logger.info("Platform registered: %s", platform)
        except Exception as e:
            logger.warning("Platform registration failed (will retry): %s", e)

    # --- Auto-seed demo data ---
    await _auto_seed()

    # --- FSM Storage ---
    # Use a separate Redis connection for FSM (needs decode_responses=True)
    storage = RedisStorage(redis)

    # --- Bot + Dispatcher ---
    if not settings.bot_token or settings.bot_token == "test":
        logger.error(
            "BOT_TOKEN is not set or invalid. "
            "Get a token from @BotFather in Telegram and set it in .env"
        )
        raise SystemExit(1)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=storage)

    # --- Middlewares ---
    # Order matters: throttle -> db -> platform -> reconcile
    # Applied to both message and callback_query updates.
    dp.message.middleware(ThrottleMiddleware(redis=redis, rate_limit=settings.rate_limit_per_minute))
    dp.callback_query.middleware(ThrottleMiddleware(redis=redis, rate_limit=settings.rate_limit_per_minute))

    dp.message.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())

    dp.message.middleware(PlatformMiddleware(platform=platform))
    dp.callback_query.middleware(PlatformMiddleware(platform=platform))

    dp.message.middleware(ReconcileMiddleware())
    dp.callback_query.middleware(ReconcileMiddleware())

    # --- Routers ---
    # Registration order matters: more specific routers first.
    # start_router handles /start (CommandStart filter) so it goes first.
    # profiling handles onboard_nl_profile + onboard_confirm states.
    # expert handles expert_dashboard + expert_evaluation states.
    # detail handles view_detail state.
    # support handles support_chat state.
    # program handles view_program state (broadest text handler).
    # fallback_router MUST be last: global /help, /support, /rebuild,
    # catch-all for messages without state, and stale callbacks.
    dp.include_router(start_router)
    dp.include_router(profiling_router)
    dp.include_router(expert_router)
    dp.include_router(detail_router)
    dp.include_router(support_router)
    dp.include_router(program_router)
    dp.include_router(fallback_router)

    # Group router for organizer replies (separate chat)
    if settings.organizer_chat_id:
        dp.include_router(support_group_router)

    # --- Health endpoint ---
    health_runner = await run_health_server()

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # --- Start polling ---
    logger.info("Starting polling...")
    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )

    # Wait for shutdown signal
    await shutdown_event.wait()

    logger.info("Shutting down...")

    # Stop polling
    await dp.stop_polling()
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    # Cleanup
    await health_runner.cleanup()
    await platform.close()
    await redis.aclose()
    await bot.session.close()

    logger.info("EventAI Agent stopped.")


if __name__ == "__main__":
    asyncio.run(main())
