"""Router: /start command with smart re-entry, role selection, shortcut.

Transitions:
- /start -> check deep link (expert) / check existing profile / check expert / fresh start
- role:guest:* / role:business -> onboard_nl_profile
- role:shortcut -> view_program (all projects, no profiling)
"""

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.keyboards.program import program_keyboard
from src.bot.keyboards.roles import role_keyboard
from src.bot.states import BotStates
from src.models.event import Event
from src.models.expert import Expert
from src.models.guest_profile import GuestProfile
from src.models.project import Project
from src.models.recommendation import Recommendation
from src.models.user import User
from src.services.expert import get_expert_by_invite

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: AsyncSession) -> None:
    """Smart re-entry: deep link, existing profile, expert, or fresh start."""
    tg_user_id = str(message.from_user.id)
    args = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""

    # Get or create user
    result = await db.execute(select(User).where(User.telegram_user_id == tg_user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            telegram_user_id=tg_user_id,
            full_name=message.from_user.full_name or "User",
            username=message.from_user.username,
        )
        db.add(user)
        await db.flush()

    # Get active event
    event_result = await db.execute(
        select(Event).where(Event.is_active.is_(True)).limit(1)
    )
    event = event_result.scalar_one_or_none()
    if not event:
        await message.answer("Нет активных мероприятий.")
        return

    await state.update_data(user_id=str(user.id), event_id=str(event.id))

    # Expert deep link: /start expert_<code>
    if args.startswith("expert_"):
        await _handle_expert_link(message, state, db, user, event, args[7:])
        return

    # Check existing expert
    expert_result = await db.execute(select(Expert).where(Expert.user_id == user.id))
    expert = expert_result.scalar_one_or_none()
    if expert and expert.bot_started:
        await state.set_state(BotStates.expert_dashboard)
        await state.update_data(expert_id=str(expert.id))
        from src.bot.routers.expert import show_dashboard

        await show_dashboard(message, state, db)
        return

    # Check existing guest profile for this event
    profile_result = await db.execute(
        select(GuestProfile).where(
            GuestProfile.user_id == user.id,
            GuestProfile.event_id == event.id,
        )
    )
    profile = profile_result.scalars().first()
    if profile:
        await _return_to_program(message, state, db, profile, event)
        return

    # Fresh start
    await state.set_state(BotStates.choose_role)
    await message.answer(
        "Привет! Я AI-куратор Demo Day.\n\n"
        "Помогу найти интересные проекты и составить программу.\n\n"
        "Выберите роль:",
        reply_markup=role_keyboard(),
    )


async def _handle_expert_link(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    event: Event,
    invite_code: str,
) -> None:
    """Process expert deep link."""
    expert = await get_expert_by_invite(db, invite_code)
    if not expert:
        await message.answer("Приглашение недействительно. Обратитесь к организатору.")
        await state.set_state(BotStates.choose_role)
        await message.answer("Выберите роль:", reply_markup=role_keyboard())
        return

    if not expert.bot_started:
        expert.bot_started = True
        expert.user_id = user.id
        await db.flush()

    user.role_code = "expert"
    await db.flush()

    await state.set_state(BotStates.expert_dashboard)
    await state.update_data(expert_id=str(expert.id))

    from src.bot.routers.expert import show_dashboard

    await show_dashboard(message, state, db)


async def _return_to_program(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    profile: GuestProfile,
    event: Event,
) -> None:
    """Return existing user to their program."""
    recs_result = await db.execute(
        select(Recommendation)
        .where(Recommendation.profile_id == profile.id)
        .order_by(Recommendation.rank)
    )
    recs = list(recs_result.scalars().all())

    await state.set_state(BotStates.view_program)
    await state.update_data(profile_id=str(profile.id))

    if recs:
        from src.bot.routers.program import format_program

        text = await format_program(recs, db)
        await message.answer(
            f"С возвращением!\n\n{text}",
            reply_markup=program_keyboard(),
        )
    else:
        await message.answer(
            "Профиль найден, но рекомендации устарели. Используйте /rebuild."
        )


@router.callback_query(BotStates.choose_role, F.data.startswith("role:"))
async def role_chosen(callback: CallbackQuery, state: FSMContext, db: AsyncSession) -> None:
    """Handle role selection callback."""
    await callback.answer()

    data_parts = callback.data.split(":")
    state_data = await state.get_data()
    user_id = state_data["user_id"]
    event_id = state_data["event_id"]

    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one()

    # Shortcut: show all projects without profiling
    if data_parts[1] == "shortcut":
        await _handle_shortcut(callback, state, db, user, event_id)
        return

    # Set role and subrole
    if data_parts[1] == "business":
        user.role_code = "business"
        user.subrole = None
    else:
        user.role_code = "guest"
        user.subrole = data_parts[2] if len(data_parts) > 2 else "other"
    await db.flush()

    # Transition to NL profiling
    await state.set_state(BotStates.onboard_nl_profile)
    await state.update_data(nl_conversation=[], nl_turn=0)

    await callback.message.edit_text(
        "Расскажите о ваших интересах, и я подберу проекты."
    )


async def _handle_shortcut(
    callback: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    event_id: str,
) -> None:
    """Show all projects without profiling."""
    user.role_code = "guest"
    await db.flush()

    await state.set_state(BotStates.view_program)
    await callback.message.edit_text("Загружаю все проекты...")

    projects_result = await db.execute(
        select(Project).where(Project.event_id == event_id).order_by(Project.title)
    )
    projects = list(projects_result.scalars().all())

    lines = ["Все проекты:\n"]
    for i, p in enumerate(projects[:20], 1):
        tags = ", ".join(p.tags[:3]) if p.tags else ""
        lines.append(f"#{i} {p.title}")
        if tags:
            lines.append(f"  {tags}")
    if len(projects) > 20:
        lines.append(f"\n...и еще {len(projects) - 20} проектов")
    lines.append("\nНапишите номер проекта для подробностей.")
    lines.append("Используйте /rebuild для персональных рекомендаций.")

    await callback.message.answer("\n".join(lines), reply_markup=program_keyboard())
