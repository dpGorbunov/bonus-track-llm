"""Router: view_detail state - project detail view.

Handles:
- Show project detail card on entry
- "Назад" button -> view_program
- "Вопросы к проекту" button -> generate questions via direct LLM call
"""

import json
import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.keyboards.program import detail_keyboard, program_keyboard, project_buttons_keyboard
from src.bot.states import BotStates
from src.models.guest_profile import GuestProfile
from src.models.project import Project
from src.models.recommendation import Recommendation
from src.models.schedule_slot import ScheduleSlot
from src.models.room import Room
from src.models.user import User
from src.prompts.qa import build_business_qa_prompt, build_guest_qa_prompt
from src.services.platform_client import PlatformClient

logger = logging.getLogger(__name__)
router = Router()


async def show_project_detail(
    target: Message | CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    project_rank: int,
) -> None:
    """Show project detail card and transition to view_detail.

    Called from program router or internally when user requests a project.
    """
    state_data = await state.get_data()
    profile_id = state_data.get("profile_id")

    # Find recommendation by rank
    rec = None
    if profile_id:
        rec_result = await db.execute(
            select(Recommendation).where(
                Recommendation.profile_id == profile_id,
                Recommendation.rank == project_rank,
            )
        )
        rec = rec_result.scalar_one_or_none()

    if not rec:
        msg = "Проект не найден в рекомендациях."
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
        else:
            await target.answer(msg)
        return

    # Load project
    proj_result = await db.execute(
        select(Project).where(Project.id == rec.project_id)
    )
    project = proj_result.scalar_one_or_none()
    if not project:
        msg = "Проект не найден."
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
        else:
            await target.answer(msg)
        return

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
            end_str = slot.end_time.strftime("%H:%M")
            slot_info = f"\nВремя: {time_str}-{end_str} | Зал: {room_name}"

    # Format card
    card_lines = [
        f"#{rec.rank}. {project.title}",
    ]

    if slot_info:
        card_lines.append(slot_info)

    card_lines.append("")

    if project.description:
        desc = project.description[:500]
        card_lines.append(desc)
        card_lines.append("")

    if project.tags:
        card_lines.append(f"Теги: {', '.join(project.tags)}")
    if project.tech_stack:
        card_lines.append(f"Стек: {', '.join(project.tech_stack)}")
    if project.track:
        card_lines.append(f"Трек: {project.track}")

    # Parsed content extras
    if project.parsed_content and isinstance(project.parsed_content, dict):
        pc = project.parsed_content
        if pc.get("problem"):
            card_lines.append(f"\nПроблема: {pc['problem']}")
        if pc.get("solution"):
            card_lines.append(f"Решение: {pc['solution']}")
        if pc.get("novelty"):
            card_lines.append(f"Новизна: {pc['novelty']}")

    if project.author:
        card_lines.append(f"\nАвтор: {project.author}")
    if project.telegram_contact:
        card_lines.append(f"Контакт: {project.telegram_contact}")
    if project.github_url:
        card_lines.append(f"GitHub: {project.github_url}")
    if project.presentation_url:
        card_lines.append(f"Презентация: {project.presentation_url}")

    card_text = "\n".join(card_lines)

    # Save to state and transition
    await state.set_state(BotStates.view_detail)
    await state.update_data(
        current_project_id=str(project.id),
        current_project_rank=project_rank,
        current_project_title=project.title,
    )

    if isinstance(target, CallbackQuery):
        await target.message.answer(card_text, reply_markup=detail_keyboard(project_rank))
    else:
        await target.answer(card_text, reply_markup=detail_keyboard(project_rank))


@router.callback_query(BotStates.view_detail, F.data == "cmd:back")
async def cb_back_to_program(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Return to program view."""
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


@router.callback_query(BotStates.view_detail, F.data.startswith("questions:"))
async def cb_generate_questions(
    callback: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    platform: PlatformClient,
) -> None:
    """Generate Q&A questions for the current project via direct LLM call."""
    await callback.answer("Генерирую вопросы...")

    rank_str = callback.data.split(":")[1]
    try:
        project_rank = int(rank_str)
    except ValueError:
        await callback.message.answer("Неверный номер проекта.")
        return

    state_data = await state.get_data()
    user_id = state_data.get("user_id")
    profile_id = state_data.get("profile_id")

    # Load project
    project_id = state_data.get("current_project_id")
    if not project_id:
        await callback.message.answer("Проект не найден.")
        return

    proj_result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = proj_result.scalar_one_or_none()
    if not project:
        await callback.message.answer("Проект не найден.")
        return

    # Load user and profile for context
    user = None
    if user_id:
        user_result = await db.execute(select(User).where(User.id == UUID(user_id)))
        user = user_result.scalar_one_or_none()

    profile = None
    if profile_id:
        prof_result = await db.execute(
            select(GuestProfile).where(GuestProfile.id == profile_id)
        )
        profile = prof_result.scalar_one_or_none()

    tech_stack_str = ", ".join(project.tech_stack or [])

    # Build prompt based on role
    is_business = user and user.role_code == "business"
    if is_business and profile:
        objective = (profile.business_objectives or ["technology"])[0]
        industries = ", ".join(profile.selected_tags or [])
        system_prompt, user_prompt = build_business_qa_prompt(
            objective=objective,
            industries=industries,
            tech_stack=tech_stack_str,
            project_title=project.title,
            project_description=project.description[:500],
            project_tech_stack=tech_stack_str,
        )
    else:
        subtype = (user.subrole if user else None) or "other"
        interests = ", ".join(
            (profile.selected_tags or []) if profile else []
        )
        system_prompt, user_prompt = build_guest_qa_prompt(
            subtype=subtype,
            interests=interests,
            project_title=project.title,
            project_description=project.description[:500],
            project_tech_stack=tech_stack_str,
        )

    # Direct LLM call (not through PydanticAI)
    try:
        resp = await platform.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        content = resp["choices"][0]["message"]["content"]
        data = json.loads(content)
        questions = data.get("questions", [])

        if not questions:
            await callback.message.answer("Не удалось сгенерировать вопросы.")
            return

        lines = [
            f"Вопросы для проекта #{project_rank}:",
            f"{project.title}",
            "",
        ]
        for i, q in enumerate(questions[:5], 1):
            lines.append(f"{i}. {q}")

        await callback.message.answer("\n".join(lines))

    except json.JSONDecodeError:
        logger.error("Q&A LLM returned non-JSON")
        await callback.message.answer("Не удалось сгенерировать вопросы.")
    except Exception as e:
        logger.error("Q&A generation failed: %s", e)
        await callback.message.answer(
            "Не удалось сгенерировать вопросы. Попробуйте позже."
        )


@router.message(BotStates.view_detail, F.text)
async def detail_text(message: Message, state: FSMContext) -> None:
    """Handle text in detail view: hint user to use buttons or go back."""
    state_data = await state.get_data()
    rank = state_data.get("current_project_rank", "?")
    await message.answer(
        f"Вы просматриваете проект #{rank}.\n"
        "Используйте кнопки ниже или напишите /start для начала.",
        reply_markup=detail_keyboard(int(rank) if isinstance(rank, int) else 1),
    )
