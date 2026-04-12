"""Router: support_chat state - communication with event organizer.

Handles:
- Confirmation message on entry
- Forward user messages to ORGANIZER_CHAT_ID with correlation_id
- Rate limit: 3 msg/5min in support
- "Назад к программе" button -> view_program
- Handle organizer replies (group messages with correlation_id)
"""

import logging
import time
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.keyboards.program import program_keyboard, project_buttons_keyboard, support_back_keyboard
from src.bot.states import BotStates
from src.core.config import settings
from src.models.recommendation import Recommendation
from src.models.support_log import SupportLog
from src.models.user import User

logger = logging.getLogger(__name__)
router = Router()

# Support-specific rate limit
SUPPORT_RATE_LIMIT = 3
SUPPORT_RATE_WINDOW = 300  # 5 minutes in seconds


@router.callback_query(BotStates.view_program, F.data == "support:start")
async def cb_support_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Enter support chat from program view."""
    await callback.answer()
    await state.set_state(BotStates.support_chat)
    await callback.message.answer(
        "Вы в режиме чата с организатором.\n"
        "Напишите свой вопрос, и мы передадим его организатору.\n"
        "Лимит: 3 сообщения за 5 минут.",
        reply_markup=support_back_keyboard(),
    )


@router.message(BotStates.support_chat, F.text)
async def support_text(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
) -> None:
    """Forward user message to organizer chat."""
    state_data = await state.get_data()
    user_id = state_data.get("user_id")
    event_id = state_data.get("event_id")

    if not user_id or not event_id:
        await message.answer("Сессия потеряна. Используйте /start.")
        return

    # Support-specific rate limit
    support_timestamps: list[float] = state_data.get("support_timestamps", [])
    now = time.time()

    # Remove expired timestamps
    support_timestamps = [
        ts for ts in support_timestamps if now - ts < SUPPORT_RATE_WINDOW
    ]

    if len(support_timestamps) >= SUPPORT_RATE_LIMIT:
        remaining = int(SUPPORT_RATE_WINDOW - (now - support_timestamps[0]))
        await message.answer(
            f"Лимит сообщений в поддержку ({SUPPORT_RATE_LIMIT}/5мин). "
            f"Подождите {remaining} сек.",
            reply_markup=support_back_keyboard(),
        )
        return

    support_timestamps.append(now)
    await state.update_data(support_timestamps=support_timestamps)

    # Save to DB via service
    from src.services.support import create_support_entry
    support_log = await create_support_entry(db, UUID(user_id), UUID(event_id), message.text)
    correlation_id = support_log.correlation_id

    # Forward to organizer chat
    organizer_chat_id = settings.organizer_chat_id
    if organizer_chat_id:
        # Load user info for context
        user_result = await db.execute(select(User).where(User.id == UUID(user_id)))
        user = user_result.scalar_one_or_none()
        user_name = user.full_name if user else "Неизвестный"
        username = f"@{user.username}" if user and user.username else ""

        forward_text = (
            f"[{correlation_id}] Вопрос от {user_name} {username}\n"
            f"---\n"
            f"{message.text}"
        )

        try:
            await message.bot.send_message(
                chat_id=organizer_chat_id,
                text=forward_text,
            )
        except Exception as e:
            logger.error("Failed to forward to organizer chat: %s", e)
            await message.answer(
                "Не удалось переслать сообщение организатору. Попробуйте позже.",
                reply_markup=support_back_keyboard(),
            )
            return

    await message.answer(
        f"Сообщение отправлено организатору (ID: {correlation_id}).\n"
        "Ответ придет в этот чат.",
        reply_markup=support_back_keyboard(),
    )


@router.callback_query(BotStates.support_chat, F.data == "support:back")
async def cb_support_back(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Return from support to program view."""
    await callback.answer()
    await state.set_state(BotStates.view_program)

    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")

    if profile_id:
        recs_result = await db.execute(
            select(Recommendation)
            .where(Recommendation.profile_id == profile_id)
            .order_by(Recommendation.rank)
        )
        recs = list(recs_result.scalars().all())

        if recs:
            from src.bot.routers.program import format_program

            text, project_list = await format_program(recs, db)
            keyboard = project_buttons_keyboard(project_list) if project_list else program_keyboard()
            await callback.message.answer(text, reply_markup=keyboard)
            return

    await callback.message.answer(
        "Назад к программе.", reply_markup=program_keyboard()
    )


# -----------------------------------------------------------------------
# Group message handler for organizer replies
# -----------------------------------------------------------------------

group_router = Router()


@group_router.message(F.chat.id == settings.organizer_chat_id, F.text)
async def organizer_reply(message: Message, db: AsyncSession) -> None:
    """Handle organizer reply in the group chat.

    Expect format: reply to a forwarded message, or message starting with
    correlation_id (sup_XXXXXXXXXXXX).
    """
    text = message.text or ""

    # Try to extract correlation_id from the replied-to message
    correlation_id = None

    if message.reply_to_message and message.reply_to_message.text:
        replied_text = message.reply_to_message.text
        for line in replied_text.split("\n"):
            if "SQ-" in line:
                import re
                match = re.search(r"(SQ-[a-f0-9]+)", line)
                if match:
                    correlation_id = match.group(1)
                    break

    # Or extract from message text itself
    if not correlation_id:
        import re
        match = re.search(r"(SQ-[a-f0-9]+)", text)
        if match:
            correlation_id = match.group(1)
            text = text.replace(correlation_id, "").strip()

    if not correlation_id:
        return  # Not a reply to a support question

    # Find support log
    result = await db.execute(
        select(SupportLog).where(SupportLog.correlation_id == correlation_id)
    )
    support_log = result.scalar_one_or_none()
    if not support_log:
        logger.warning("Support log not found for correlation_id=%s", correlation_id)
        return

    # Update answer in DB
    from datetime import datetime, timezone

    support_log.answer = text
    support_log.answered_at = datetime.now(timezone.utc)
    await db.flush()

    # Find user's telegram_user_id
    user_result = await db.execute(
        select(User).where(User.id == support_log.user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        logger.error("User not found for support log %s", support_log.id)
        return

    # Send reply to user
    reply_text = (
        f"Ответ от организатора:\n"
        f"---\n"
        f"{text}"
    )

    try:
        await message.bot.send_message(
            chat_id=int(user.telegram_user_id),
            text=reply_text,
        )
        await message.reply("Ответ отправлен пользователю.")
    except Exception as e:
        logger.error("Failed to send reply to user %s: %s", user.telegram_user_id, e)
        await message.reply(f"Не удалось отправить ответ: {e}")
