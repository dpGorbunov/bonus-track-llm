"""Router: view_program state - main agent interaction.

Handles:
- User text -> PydanticAI agent run (timeout=15s)
- /profile command -> show profile
- /rebuild command -> reset to profiling
- /support command -> support chat
- cmd:if_time callback -> show if_time recommendations
- cmd:profile callback -> show profile
- Agent tool calls that transition state (show_project -> view_detail)
"""

import asyncio
import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.agent import AgentDeps, create_agent
from src.core.config import settings
from src.core.sanitize import sanitize_text
from src.bot.keyboards.program import detail_keyboard, program_keyboard, project_buttons_keyboard
from src.bot.states import BotStates
from src.models.chat_message import ChatMessage
from src.models.event import Event
from src.models.guest_profile import GuestProfile
from src.models.project import Project
from src.models.recommendation import Recommendation
from src.models.schedule_slot import ScheduleSlot
from src.models.room import Room
from src.models.user import User
from src.services.platform_client import PlatformClient

logger = logging.getLogger(__name__)
router = Router()

# Chat history limit per user
MAX_CHAT_HISTORY = 20


@router.message(BotStates.view_program, Command("profile"))
async def cmd_profile(message: Message, state: FSMContext, db: AsyncSession) -> None:
    """Show user profile directly."""
    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")
    if not profile_id:
        await message.answer("Профиль не найден. Используйте /start.")
        return

    result = await db.execute(
        select(GuestProfile).where(GuestProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        await message.answer("Профиль не найден.")
        return

    await message.answer(_format_profile_text(profile))


@router.message(BotStates.view_program, Command("rebuild"))
async def cmd_rebuild(
    message: Message, state: FSMContext, db: AsyncSession
) -> None:
    """Clear profile and restart profiling."""
    from sqlalchemy import delete

    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")

    # Delete old recommendations and profile from DB
    if profile_id:
        await db.execute(delete(Recommendation).where(Recommendation.profile_id == UUID(profile_id)))
        await db.execute(delete(GuestProfile).where(GuestProfile.id == UUID(profile_id)))
        await db.flush()

    await state.update_data(
        nl_conversation=[],
        nl_turn=0,
        extracted_profile=None,
        program_chat=[],
        profile_id=None,
    )
    await state.set_state(BotStates.onboard_nl_profile)
    await message.answer("Давайте пересоздадим профиль. Расскажите о ваших интересах.")


@router.message(BotStates.view_program, Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    """Transition to support chat."""
    from src.bot.keyboards.program import support_back_keyboard

    await state.set_state(BotStates.support_chat)
    await message.answer(
        "Вы в режиме чата с организатором.\n"
        "Напишите свой вопрос, и мы передадим его организатору.\n"
        "Лимит: 3 сообщения за 5 минут.",
        reply_markup=support_back_keyboard(),
    )


@router.callback_query(BotStates.view_program, F.data == "cmd:profile")
async def cb_profile(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Show profile via button."""
    await callback.answer()

    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")
    if not profile_id:
        await callback.message.answer("Профиль не найден.")
        return

    result = await db.execute(
        select(GuestProfile).where(GuestProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        await callback.message.answer("Профиль не найден.")
        return

    await callback.message.answer(_format_profile_text(profile))


@router.callback_query(BotStates.view_program, F.data == "cmd:if_time")
async def cb_if_time(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Show if_time recommendations."""
    await callback.answer()

    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")
    if not profile_id:
        await callback.message.answer("Рекомендации не найдены.")
        return

    recs_result = await db.execute(
        select(Recommendation)
        .where(
            Recommendation.profile_id == profile_id,
            Recommendation.category == "if_time",
        )
        .order_by(Recommendation.rank)
    )
    recs = list(recs_result.scalars().all())

    if not recs:
        await callback.message.answer("Нет дополнительных рекомендаций.")
        return

    text, _ = await format_program(recs, db, header="Если успеете:")
    await callback.message.answer(text)


@router.callback_query(BotStates.view_program, F.data.startswith("project:"))
async def cb_project_detail(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Open project detail by inline button."""
    await callback.answer()
    try:
        rank = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.message.answer("Неверный номер проекта.")
        return
    from src.bot.routers.detail import show_project_detail

    await show_project_detail(callback, state, db, rank)


@router.callback_query(BotStates.view_program, F.data == "cmd:export_pdf")
async def cb_export_pdf(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Export recommendations as PDF document."""
    await callback.answer("Генерирую PDF...")

    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")
    user_id = state_data.get("user_id")

    if not profile_id:
        await callback.message.answer("Нет рекомендаций для экспорта.")
        return

    recs_result = await db.execute(
        select(Recommendation)
        .where(Recommendation.profile_id == UUID(profile_id))
        .order_by(Recommendation.rank)
    )
    recs = list(recs_result.scalars().all())

    if not recs:
        await callback.message.answer("Нет рекомендаций для экспорта.")
        return

    project_ids = [r.project_id for r in recs]
    proj_result = await db.execute(
        select(Project).where(Project.id.in_(project_ids))
    )
    projects = list(proj_result.scalars().all())

    # Get user name
    user_name = "Участник"
    if user_id:
        user_result = await db.execute(select(User).where(User.id == UUID(user_id)))
        user = user_result.scalar_one_or_none()
        if user:
            user_name = user.full_name

    from src.services.pdf_export import generate_recommendations_pdf
    from aiogram.types import BufferedInputFile

    pdf_buf = await generate_recommendations_pdf(recs, projects, user_name=user_name)
    doc = BufferedInputFile(pdf_buf.read(), filename="demo_day_program.pdf")
    await callback.message.answer_document(doc, caption="Ваша программа Demo Day")


@router.message(BotStates.view_program, F.text)
async def view_program_text(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    platform: PlatformClient,
) -> None:
    """Main agent interaction: user text -> PydanticAI agent -> response."""
    state_data = await state.get_data()
    user_id = state_data.get("user_id")
    event_id = state_data.get("event_id")
    profile_id = state_data.get("profile_id")

    if not user_id or not event_id:
        await message.answer("Сессия потеряна. Используйте /start.")
        return

    # Load dependencies
    user_result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = user_result.scalar_one_or_none()
    if not user:
        await message.answer("Пользователь не найден. Используйте /start.")
        return

    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        await message.answer("Мероприятие не найдено.")
        return

    profile = None
    if profile_id:
        result = await db.execute(
            select(GuestProfile).where(GuestProfile.id == profile_id)
        )
        profile = result.scalar_one_or_none()

    # Load recommendations
    recs: list[Recommendation] = []
    if profile:
        recs_result = await db.execute(
            select(Recommendation)
            .where(Recommendation.profile_id == profile.id)
            .order_by(Recommendation.rank)
        )
        recs = list(recs_result.scalars().all())

    # Truncate long messages
    user_text = message.text or ""
    if len(user_text) > 2000:
        user_text = user_text[:2000]
        await message.answer("Сообщение обрезано до 2000 символов.")

    # Save user message to chat history
    safe_text = sanitize_text(user_text) or ""
    chat_msg = ChatMessage(
        user_id=UUID(user_id),
        event_id=UUID(event_id),
        role="user",
        content=safe_text,
    )
    db.add(chat_msg)
    await db.flush()

    # Build and run agent
    deps = AgentDeps(
        platform=platform,
        db=db,
        user=user,
        profile=profile,
        recommendations=recs,
        event=event,
    )

    # Load chat history from state (before try block so it's always defined)
    program_chat: list[dict] = state_data.get("program_chat", [])
    program_chat.append({"role": "user", "content": message.text})

    try:
        agent = create_agent(platform.platform_url, platform.token)

        # Trim history
        if len(program_chat) > MAX_CHAT_HISTORY:
            program_chat = program_chat[-MAX_CHAT_HISTORY:]

        # Run agent with timeout
        agent_result = await asyncio.wait_for(
            agent.run(
                message.text,
                deps=deps,
                message_history=[
                    _to_pydantic_message(m) for m in program_chat[:-1]
                ] if len(program_chat) > 1 else None,
            ),
            timeout=settings.agent_timeout,
        )

        reply_text = agent_result.output
        if not reply_text:
            reply_text = "Не удалось получить ответ. Попробуйте переформулировать."

    except asyncio.TimeoutError:
        reply_text = "Обработка занимает больше времени. Попробуйте еще раз."
        logger.warning("Agent timeout for user %s", user_id)
    except Exception as e:
        reply_text = "Произошла ошибка. Попробуйте еще раз или используйте кнопки."
        logger.error("Agent error for user %s: %s", user_id, e)

    # Save assistant reply (continue using the same program_chat built above)
    program_chat.append({"role": "assistant", "content": reply_text})

    if len(program_chat) > MAX_CHAT_HISTORY:
        program_chat = program_chat[-MAX_CHAT_HISTORY:]

    await state.update_data(program_chat=program_chat)

    # Save to DB
    assistant_msg = ChatMessage(
        user_id=UUID(user_id),
        event_id=UUID(event_id),
        role="assistant",
        content=sanitize_text(reply_text) or reply_text,
    )
    db.add(assistant_msg)
    await db.flush()

    # Send reply, split long messages
    await _safe_send(message, reply_text)


def _to_pydantic_message(msg: dict):
    """Convert dict message to PydanticAI ModelMessage format."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart

    if msg["role"] == "user":
        return ModelRequest(parts=[UserPromptPart(content=msg["content"])])
    return ModelResponse(parts=[TextPart(content=msg["content"])])


async def format_program(
    recs: list[Recommendation],
    db: AsyncSession,
    header: str = "Ваша программа:",
) -> tuple[str, list[tuple[int, str]]]:
    """Format recommendations list with schedule info.

    Returns (text, [(rank, title), ...]).
    """
    lines = [header, ""]
    project_list: list[tuple[int, str]] = []

    for rec in recs:
        # Load project
        proj_result = await db.execute(
            select(Project).where(Project.id == rec.project_id)
        )
        project = proj_result.scalar_one_or_none()
        if not project:
            continue

        project_list.append((rec.rank, project.title))

        # Load schedule slot
        slot_info = ""
        if rec.slot_id:
            slot_result = await db.execute(
                select(ScheduleSlot, Room.name.label("room_name"))
                .join(Room, ScheduleSlot.room_id == Room.id)
                .where(ScheduleSlot.id == rec.slot_id)
            )
            row = slot_result.first()
            if row:
                slot = row[0]
                room_name = row.room_name
                time_str = slot.start_time.strftime("%H:%M")
                slot_info = f" | {time_str} | {room_name}"

        category_marker = ""
        if rec.category == "if_time":
            category_marker = " [если успеете]"

        tags = ", ".join(project.tags[:3]) if project.tags else ""

        lines.append(f"#{rec.rank} {project.title}{slot_info}{category_marker}")
        if tags:
            lines.append(f"   {tags}")

    return "\n".join(lines), project_list


def _format_profile_text(profile: GuestProfile) -> str:
    """Format guest profile for display."""
    parts = ["Ваш профиль:\n"]
    if profile.selected_tags:
        parts.append(f"Интересы: {', '.join(profile.selected_tags)}")
    if profile.keywords:
        parts.append(f"Ключевые слова: {', '.join(profile.keywords)}")
    if profile.nl_summary:
        parts.append(f"\n{profile.nl_summary}")
    if profile.company:
        parts.append(f"\nКомпания: {profile.company}")
    if profile.position:
        parts.append(f"Должность: {profile.position}")
    if profile.business_objectives:
        parts.append(f"Бизнес-цели: {', '.join(profile.business_objectives)}")
    return "\n".join(parts)


async def _safe_send(message: Message, text: str, **_kwargs) -> None:
    """Send LLM text with Telegram-safe formatting via entities."""
    from src.core.telegram_format import send_formatted
    await send_formatted(message, text)
