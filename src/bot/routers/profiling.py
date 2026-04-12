"""Router: NL profiling and profile confirmation.

States: onboard_nl_profile, onboard_confirm.

Flow:
1. User text -> chat_for_profile (LLM)
2. action=reply -> send reply, stay in onboard_nl_profile
3. action=profile -> show extracted profile, transition to onboard_confirm
4. Confirm -> create GuestProfile -> generate recommendations -> view_program
5. Retry -> reset conversation, back to onboard_nl_profile
"""

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.keyboards.program import confirm_profile_keyboard, program_keyboard
from src.bot.states import BotStates
from src.core.sanitize import sanitize_text
from src.models.event import Event
from src.models.guest_profile import GuestProfile
from src.models.project import Project
from src.models.recommendation import Recommendation
from src.models.user import User
from src.prompts.profiling import get_profile_agent_system, get_role_context
from src.services.platform_client import PlatformClient
from src.services.profiling import build_profile_text, chat_for_profile
from src.services.retriever import generate_recommendations

logger = logging.getLogger(__name__)
router = Router()

# Maximum LLM turns before forcing profile extraction
MAX_NL_TURNS = 3


@router.message(BotStates.onboard_nl_profile, F.text)
async def nl_profile_text(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    platform: PlatformClient,
) -> None:
    """Handle free-text input during NL profiling."""
    state_data = await state.get_data()
    user_id = state_data["user_id"]
    event_id = state_data["event_id"]
    nl_conversation: list[dict] = state_data.get("nl_conversation", [])
    nl_turn: int = state_data.get("nl_turn", 0)

    # Load user for role context
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сессия устарела. Используйте /start.")
        await state.clear()
        return

    # Load event for tag list
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    if not event:
        await message.answer("Мероприятие не найдено. Используйте /start.")
        await state.clear()
        return

    # Build tag list from event projects
    tag_list = await _get_tag_list(db, UUID(event_id))

    # Append user message
    nl_conversation.append({"role": "user", "content": message.text})

    # Build system prompt
    role_context = get_role_context(user.role_code, user.subrole)
    system_prompt = get_profile_agent_system(tag_list, role_context)

    # Call LLM
    llm_result = await chat_for_profile(platform, system_prompt, nl_conversation)
    action = llm_result.get("action", "reply")

    # Guard: force reply on first turn (at least 1 clarifying question)
    assistant_turns = sum(1 for m in nl_conversation if m["role"] == "assistant")
    if action == "profile" and assistant_turns == 0 and nl_turn == 0:
        action = "reply"
        llm_result["action"] = "reply"
        if not llm_result.get("message"):
            llm_result["message"] = "Расскажите подробнее о ваших интересах."

    if action == "reply":
        reply_text = llm_result.get("message", "Расскажите подробнее.")
        nl_conversation.append({"role": "assistant", "content": reply_text})
        await state.update_data(
            nl_conversation=nl_conversation,
            nl_turn=nl_turn + 1,
        )
        await message.answer(reply_text)
        return

    # action == "profile" -> show for confirmation
    interests = llm_result.get("interests", [])
    goals = llm_result.get("goals", [])
    summary = llm_result.get("summary", "")

    # Business-specific fields
    company = llm_result.get("company")
    position = llm_result.get("position")
    business_objectives = llm_result.get("business_objectives")

    # Store extracted profile in state for confirmation
    await state.update_data(
        nl_conversation=nl_conversation,
        extracted_profile={
            "interests": interests,
            "goals": goals,
            "summary": summary,
            "company": company,
            "position": position,
            "business_objectives": business_objectives,
            "raw_text": message.text,
        },
    )

    # Format confirmation message
    confirm_lines = ["Ваш профиль:\n"]
    if interests:
        confirm_lines.append(f"Интересы: {', '.join(interests)}")
    if goals:
        confirm_lines.append(f"Цели: {', '.join(goals)}")
    if summary:
        confirm_lines.append(f"\n{summary}")
    if company:
        confirm_lines.append(f"\nКомпания: {company}")
    if position:
        confirm_lines.append(f"Должность: {position}")
    if business_objectives:
        confirm_lines.append(f"Бизнес-цели: {', '.join(business_objectives)}")
    confirm_lines.append("\nВсе верно?")

    await state.set_state(BotStates.onboard_confirm)
    await message.answer(
        "\n".join(confirm_lines),
        reply_markup=confirm_profile_keyboard(),
    )


@router.callback_query(BotStates.onboard_confirm, F.data == "profile:confirm")
async def profile_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    platform: PlatformClient,
) -> None:
    """Confirm extracted profile -> create GuestProfile -> generate recommendations."""
    await callback.answer()

    state_data = await state.get_data()
    user_id = state_data["user_id"]
    event_id = state_data["event_id"]
    extracted = state_data.get("extracted_profile", {})

    interests = extracted.get("interests", [])
    goals = extracted.get("goals", [])
    summary = extracted.get("summary", "")
    company = extracted.get("company")
    position = extracted.get("position")
    business_objectives = extracted.get("business_objectives")
    raw_text = extracted.get("raw_text")

    # Create GuestProfile
    profile = GuestProfile(
        user_id=UUID(user_id),
        event_id=UUID(event_id),
        selected_tags=interests,
        keywords=goals,
        nl_summary=sanitize_text(summary),
        raw_text=sanitize_text(raw_text),
        company=sanitize_text(company),
        position=sanitize_text(position),
        objective=goals[0] if goals else None,
        business_objectives=business_objectives,
    )
    db.add(profile)
    await db.flush()

    await state.update_data(profile_id=str(profile.id))
    await callback.message.edit_text("Профиль сохранен. Генерирую рекомендации...")

    # Generate recommendations
    profile_text = build_profile_text(
        selected_tags=interests,
        keywords=goals,
        nl_summary=summary,
        company=company,
        business_objectives=business_objectives,
        raw_text=raw_text,
    )

    recs = await generate_recommendations(
        db=db,
        platform=platform,
        profile_id=profile.id,
        event_id=UUID(event_id),
        profile_text=profile_text,
        selected_tags=interests,
    )

    await state.set_state(BotStates.view_program)

    if recs:
        from src.bot.routers.program import format_program

        text = await format_program(recs, db)
        await callback.message.answer(
            f"Ваша программа:\n\n{text}",
            reply_markup=program_keyboard(),
        )
    else:
        await callback.message.answer(
            "Не удалось сгенерировать рекомендации. Попробуйте /rebuild.",
            reply_markup=program_keyboard(),
        )


@router.callback_query(BotStates.onboard_confirm, F.data == "profile:retry")
async def profile_retry(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Retry profiling: reset conversation, back to NL profiling."""
    await callback.answer()

    await state.update_data(nl_conversation=[], nl_turn=0, extracted_profile=None)
    await state.set_state(BotStates.onboard_nl_profile)

    await callback.message.edit_text(
        "Давайте попробуем заново. Расскажите о ваших интересах."
    )


@router.message(BotStates.onboard_confirm)
async def onboard_confirm_text(message: Message, state: FSMContext) -> None:
    """Catch text in onboard_confirm - prompt to use buttons."""
    await message.answer("Нажмите кнопку 'Все верно' или 'Заново' выше.")


async def trigger_recommendations(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    platform: PlatformClient,
) -> None:
    """Re-generate recommendations for existing profile. Called from other routers."""
    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")
    event_id = state_data.get("event_id")

    if not profile_id or not event_id:
        await message.answer("Профиль не найден. Используйте /start.")
        return

    result = await db.execute(
        select(GuestProfile).where(GuestProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        await message.answer("Профиль не найден. Используйте /start.")
        return

    profile_text = build_profile_text(
        selected_tags=profile.selected_tags,
        keywords=profile.keywords,
        nl_summary=profile.nl_summary,
        company=profile.company,
        business_objectives=profile.business_objectives,
        raw_text=profile.raw_text,
    )

    recs = await generate_recommendations(
        db=db,
        platform=platform,
        profile_id=UUID(profile_id),
        event_id=UUID(event_id),
        profile_text=profile_text,
        selected_tags=profile.selected_tags,
    )

    await state.set_state(BotStates.view_program)

    if recs:
        from src.bot.routers.program import format_program

        text = await format_program(recs, db)
        await message.answer(
            f"Ваша программа:\n\n{text}",
            reply_markup=program_keyboard(),
        )
    else:
        await message.answer(
            "Не удалось сгенерировать рекомендации.",
            reply_markup=program_keyboard(),
        )


async def _get_tag_list(db: AsyncSession, event_id: UUID) -> str:
    """Collect unique tags from all projects of the event."""
    result = await db.execute(
        select(Project.tags).where(Project.event_id == event_id)
    )
    all_tags: set[str] = set()
    for (tags,) in result.all():
        if tags:
            all_tags.update(tags)

    sorted_tags = sorted(all_tags)
    return ", ".join(sorted_tags) if sorted_tags else "AI, ML, NLP, CV, LLM, Agents"
