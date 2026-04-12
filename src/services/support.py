import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.sanitize import sanitize_text
from src.models.support_log import SupportLog

logger = logging.getLogger(__name__)


def generate_correlation_id() -> str:
    """Generate short correlation ID: SQ-<6 hex chars>."""
    return f"SQ-{secrets.token_hex(3)}"


async def create_support_entry(
    db: AsyncSession,
    user_id: UUID,
    event_id: UUID,
    question: str,
) -> SupportLog:
    """Create support log entry with correlation_id."""
    entry = SupportLog(
        user_id=user_id,
        event_id=event_id,
        correlation_id=generate_correlation_id(),
        question=(sanitize_text(question) or "")[:1000],  # max 1000 chars
    )
    db.add(entry)
    await db.flush()
    return entry


async def find_by_correlation_id(
    db: AsyncSession, correlation_id: str
) -> SupportLog | None:
    result = await db.execute(
        select(SupportLog).where(
            SupportLog.correlation_id == correlation_id,
            SupportLog.answer.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def find_by_bot_message_id(
    db: AsyncSession, bot_message_id: int
) -> SupportLog | None:
    """Find support log by the bot's message ID in organizer group (for reply_to).

    Searches unanswered entries created within the last 24 hours, ordered by
    most recent first. The bot_message_id is matched against the correlation_id
    field where the message ID was stored after forwarding to the organizer group.
    """
    cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(SupportLog)
        .where(
            SupportLog.answer.is_(None),
            SupportLog.created_at >= cutoff,
        )
        .order_by(SupportLog.created_at.desc())
    )
    # Linear scan over recent unanswered entries to find matching message_id
    # stored in correlation_id after forwarding
    for entry in result.scalars().all():
        if entry.correlation_id == str(bot_message_id):
            return entry
    return None


async def save_answer(
    db: AsyncSession,
    entry: SupportLog,
    answer: str,
) -> None:
    entry.answer = answer
    entry.answered_at = datetime.now(timezone.utc)
    await db.flush()
