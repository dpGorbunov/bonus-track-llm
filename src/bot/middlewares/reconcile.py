import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.states import BotStates
from src.models.expert import Expert
from src.models.guest_profile import GuestProfile
from src.models.user import User

logger = logging.getLogger(__name__)


class ReconcileMiddleware(BaseMiddleware):
    """Reconcile FSM state with PostgreSQL on /start or missing state.

    Runs only when:
    - Current FSM state is None (user returned after bot restart), or
    - The incoming message is /start (explicit re-entry).

    Determines the correct state from DB records:
    - Expert with bot_started=True -> expert_dashboard
    - GuestProfile exists -> view_program
    - Otherwise -> let the handler decide (new user flow).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if not state:
            return await handler(event, data)

        current = await state.get_state()
        is_start = (
            isinstance(event, Message)
            and event.text is not None
            and event.text.startswith("/start")
        )

        # Only reconcile on /start or missing state
        if current is not None and not is_start:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        db: AsyncSession | None = data.get("db")
        if not db:
            return await handler(event, data)

        tg_user_id = str(user.id)

        # Expert deep link - let the handler parse the payload
        if is_start and event.text and "expert_" in event.text:
            return await handler(event, data)

        # Look up existing user in DB
        result = await db.execute(
            select(User).where(User.telegram_user_id == tg_user_id)
        )
        db_user = result.scalar_one_or_none()

        if not db_user:
            # New user - handler will set choose_role
            return await handler(event, data)

        # Check expert status
        expert_result = await db.execute(
            select(Expert).where(Expert.user_id == db_user.id)
        )
        expert = expert_result.scalar_one_or_none()
        if expert and expert.bot_started:
            if current is None:
                await state.set_state(BotStates.expert_dashboard)
                logger.info("Reconciled user %s -> expert_dashboard", tg_user_id)
            return await handler(event, data)

        # Check guest profile
        profile_result = await db.execute(
            select(GuestProfile).where(GuestProfile.user_id == db_user.id)
        )
        profile = profile_result.scalars().first()
        if profile and current is None:
            await state.set_state(BotStates.view_program)
            logger.info("Reconciled user %s -> view_program", tg_user_id)

        return await handler(event, data)
