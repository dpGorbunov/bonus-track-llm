"""Router: expert_dashboard and expert_evaluation states.

Handles:
- show_dashboard: list room projects with scoring progress
- "Оценить проект X" -> expert_evaluation with 1-5 keyboards
- Criterion-by-criterion scoring (5 criteria from event config)
- Comment input (free text)
- "Подтвердить" -> save score (ON CONFLICT UPDATE), back to dashboard
- "Назад" -> discard partial score, back to dashboard
"""

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.keyboards.expert import (
    confirm_score_keyboard,
    expert_dashboard_keyboard,
    score_keyboard,
)
from src.bot.states import BotStates
from src.core.sanitize import sanitize_text
from src.models.event import Event
from src.models.expert import Expert
from src.models.project import Project
from src.services.expert import get_expert_progress, save_score

logger = logging.getLogger(__name__)
router = Router()

# Default evaluation criteria if event doesn't define them
DEFAULT_CRITERIA = [
    "Техническая реализация",
    "Инновационность",
    "Практическая ценность",
    "Качество презентации",
    "Общее впечатление",
]


async def show_dashboard(
    target: Message | CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
) -> None:
    """Show expert dashboard with scoring progress.

    Called from start router on expert entry and from evaluation completion.
    """
    state_data = await state.get_data()
    expert_id = state_data.get("expert_id")
    event_id = state_data.get("event_id")

    if not expert_id or not event_id:
        msg = "Сессия эксперта не найдена. Используйте /start."
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
        else:
            await target.answer(msg)
        return

    # Load expert
    expert_result = await db.execute(select(Expert).where(Expert.id == expert_id))
    expert = expert_result.scalar_one_or_none()
    if not expert:
        msg = "Эксперт не найден."
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
        else:
            await target.answer(msg)
        return

    # Get evaluation criteria from event
    event_result = await db.execute(select(Event).where(Event.id == event_id))
    event = event_result.scalar_one_or_none()
    criteria = _get_criteria(event)
    await state.update_data(criteria=criteria)

    if not expert.room_id:
        msg = (
            f"Добро пожаловать, {expert.name}!\n"
            "Вам пока не назначен зал. Обратитесь к организатору."
        )
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
        else:
            await target.answer(msg)
        return

    # Get scoring progress
    progress = await get_expert_progress(
        db, UUID(expert_id), expert.room_id, UUID(event_id)
    )
    projects = progress["projects"]
    scores = progress["scores"]
    total = progress["total"]
    scored = progress["scored"]

    # Build dashboard text
    lines = [
        f"Эксперт: {expert.name}",
        f"Прогресс: {scored}/{total} проектов оценено",
        "",
    ]

    for p in projects:
        status = "v" if p.id in scores else " "
        lines.append(f"[{status}] {p.title[:40]}")

    lines.append("")
    if scored == total and total > 0:
        lines.append("Все проекты оценены!")
    else:
        lines.append("Выберите проект для оценки:")

    dashboard_text = "\n".join(lines)

    scored_ids = set(scores.keys())
    keyboard = expert_dashboard_keyboard(projects, scored_ids)

    await state.set_state(BotStates.expert_dashboard)

    if isinstance(target, CallbackQuery):
        await target.message.answer(dashboard_text, reply_markup=keyboard)
    else:
        await target.answer(dashboard_text, reply_markup=keyboard)


@router.callback_query(BotStates.expert_dashboard, F.data.startswith("eval:"))
async def cb_start_evaluation(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Start evaluating a specific project."""
    await callback.answer()

    project_id = callback.data.split(":")[1]

    # Load project
    proj_result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = proj_result.scalar_one_or_none()
    if not project:
        await callback.message.answer("Проект не найден.")
        return

    state_data = await state.get_data()
    criteria = state_data.get("criteria", DEFAULT_CRITERIA)

    # Initialize evaluation state
    await state.set_state(BotStates.expert_evaluation)
    await state.update_data(
        eval_project_id=project_id,
        eval_project_title=project.title,
        eval_scores={},
        eval_criterion_index=0,
        eval_awaiting_comment=False,
    )

    # Show first criterion
    criterion = criteria[0]
    await callback.message.answer(
        f"Оценка проекта: {project.title}\n\n"
        f"Критерий 1/{len(criteria)}: {criterion}\n"
        "Выберите оценку (1-5):",
        reply_markup=score_keyboard(0),
    )


@router.callback_query(
    BotStates.expert_evaluation,
    F.data == "score:confirm",
)
async def cb_confirm_score(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Save confirmed score and return to dashboard."""
    await callback.answer()

    state_data = await state.get_data()
    expert_id = state_data.get("expert_id")
    project_id = state_data.get("eval_project_id")
    eval_scores = state_data.get("eval_scores", {})
    comment = state_data.get("eval_comment")

    if not expert_id or not project_id:
        await callback.message.answer("Ошибка. Используйте /start.")
        return

    # Load expert for room_id
    expert_result = await db.execute(select(Expert).where(Expert.id == expert_id))
    expert = expert_result.scalar_one_or_none()
    if not expert or not expert.room_id:
        await callback.message.answer("Ошибка: зал эксперта не найден.")
        return

    saved = await save_score(
        db=db,
        expert_id=UUID(expert_id),
        project_id=UUID(project_id),
        room_id=expert.room_id,
        criteria_scores=eval_scores,
        comment=comment,
    )

    if saved:
        await callback.message.answer("Оценка сохранена.")
    else:
        await callback.message.answer(
            "Не удалось сохранить оценку. Проект не в вашем зале."
        )

    # Clean evaluation state
    _clear_eval_state(state_data)
    await state.update_data(**_eval_defaults())

    # Return to dashboard
    await show_dashboard(callback, state, db)


@router.callback_query(
    BotStates.expert_evaluation,
    F.data == "score:cancel",
)
async def cb_cancel_score(
    callback: CallbackQuery, state: FSMContext, db: AsyncSession
) -> None:
    """Discard partial score and return to dashboard."""
    await callback.answer()

    await state.update_data(**_eval_defaults())
    await callback.message.answer("Оценка отменена.")

    await show_dashboard(callback, state, db)


@router.callback_query(
    BotStates.expert_evaluation,
    F.data.startswith("score:"),
)
async def cb_score_criterion(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Handle 1-5 score selection for a criterion."""
    await callback.answer()

    parts = callback.data.split(":")
    # score:<criterion_index>:<value>
    if len(parts) != 3:
        return

    try:
        criterion_index = int(parts[1])
        score_value = int(parts[2])
    except ValueError:
        return

    if score_value < 1 or score_value > 5:
        await callback.message.answer("Оценка должна быть от 1 до 5.")
        return

    state_data = await state.get_data()
    criteria = state_data.get("criteria", DEFAULT_CRITERIA)
    eval_scores: dict = state_data.get("eval_scores", {})
    project_title = state_data.get("eval_project_title", "")

    # Save score for this criterion
    criterion_name = criteria[criterion_index]
    eval_scores[criterion_name] = score_value

    next_index = criterion_index + 1
    await state.update_data(
        eval_scores=eval_scores,
        eval_criterion_index=next_index,
    )

    if next_index < len(criteria):
        # Show next criterion
        next_criterion = criteria[next_index]
        await callback.message.edit_text(
            f"Оценка проекта: {project_title}\n\n"
            f"Критерий {next_index + 1}/{len(criteria)}: {next_criterion}\n"
            "Выберите оценку (1-5):",
            reply_markup=score_keyboard(next_index),
        )
    else:
        # All criteria scored, ask for comment
        await state.update_data(eval_awaiting_comment=True)

        # Show summary
        lines = [f"Оценки для проекта: {project_title}\n"]
        for crit, val in eval_scores.items():
            lines.append(f"  {crit}: {val}/5")
        lines.append(
            "\nНапишите комментарий (или отправьте '-' чтобы пропустить):"
        )

        await callback.message.edit_text("\n".join(lines))


@router.message(BotStates.expert_evaluation, F.text)
async def eval_comment_text(message: Message, state: FSMContext) -> None:
    """Handle comment input during evaluation."""
    state_data = await state.get_data()
    awaiting_comment = state_data.get("eval_awaiting_comment", False)

    if not awaiting_comment:
        await message.answer("Используйте кнопки для оценки.")
        return

    comment = sanitize_text(message.text)
    if comment == "-":
        comment = None

    await state.update_data(eval_comment=comment, eval_awaiting_comment=False)

    project_title = state_data.get("eval_project_title", "")
    eval_scores = state_data.get("eval_scores", {})

    # Show final summary for confirmation
    lines = [f"Итоговая оценка: {project_title}\n"]
    for crit, val in eval_scores.items():
        lines.append(f"  {crit}: {val}/5")
    if comment:
        lines.append(f"\nКомментарий: {comment}")
    lines.append("\nПодтвердить?")

    await message.answer(
        "\n".join(lines),
        reply_markup=confirm_score_keyboard(),
    )


def _eval_defaults() -> dict:
    """Return clean evaluation state defaults."""
    return {
        "eval_project_id": None,
        "eval_project_title": None,
        "eval_scores": {},
        "eval_criterion_index": 0,
        "eval_awaiting_comment": False,
        "eval_comment": None,
    }


def _clear_eval_state(state_data: dict) -> None:
    """Clear evaluation keys from state data dict (in-place, for reference)."""
    for key in (
        "eval_project_id",
        "eval_project_title",
        "eval_scores",
        "eval_criterion_index",
        "eval_awaiting_comment",
        "eval_comment",
    ):
        state_data.pop(key, None)


def _get_criteria(event: Event | None) -> list[str]:
    """Extract evaluation criteria from event config or use defaults."""
    if not event or not event.evaluation_criteria:
        return DEFAULT_CRITERIA

    criteria_data = event.evaluation_criteria

    if isinstance(criteria_data, dict) and "criteria" in criteria_data:
        criteria_list = criteria_data["criteria"]
        if isinstance(criteria_list, list) and criteria_list:
            names = []
            for c in criteria_list:
                if isinstance(c, str):
                    names.append(c)
                elif isinstance(c, dict) and "name" in c:
                    names.append(c["name"])
            if names:
                return names

    if isinstance(criteria_data, list):
        result = [str(c) for c in criteria_data if c]
        if result:
            return result

    return DEFAULT_CRITERIA
