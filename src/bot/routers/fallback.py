"""Fallback router - registered LAST in the dispatcher.

Catches commands and messages that no state-specific router handled:
- /help in any state
- /support outside view_program
- /rebuild outside view_program
- Messages when FSM state is None (user hasn't run /start)
- Outdated/invalid callback queries
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.states import BotStates

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("help"))
async def cmd_help_global(message: Message, state: FSMContext) -> None:
    """Global /help handler - works in any state."""
    current = await state.get_state()
    if current == BotStates.view_program.state:
        await message.answer(
            "Напишите вопрос текстом или используйте команды:\n"
            "/profile - показать профиль\n"
            "/rebuild - пересоздать профиль\n"
            "/support - вопрос организатору\n\n"
            "Примеры:\n"
            "- 'Покажи проект 3'\n"
            "- 'Сравни проекты 1 и 2'\n"
            "- 'Какие вопросы задать автору?'"
        )
    elif current and "expert" in current:
        await message.answer("Выберите проект для оценки из списка выше.")
    else:
        await message.answer("Используйте /start чтобы начать.")


@router.message(Command("support"))
async def cmd_support_global(message: Message, state: FSMContext) -> None:
    """Redirect to support from any state outside view_program."""
    current = await state.get_state()
    if current == BotStates.view_program.state:
        # Let the program router handle it - but since this is fallback,
        # the program router should have already handled it. If we got here
        # it means something went wrong, so provide a helpful response.
        pass
    await message.answer(
        "Команда /support доступна после получения рекомендаций. "
        "Используйте /start."
    )


@router.message(Command("rebuild"))
async def cmd_rebuild_global(message: Message, state: FSMContext) -> None:
    """Redirect to rebuild from any state outside view_program."""
    await message.answer("Используйте /start чтобы начать заново.")


@router.message()
async def fallback_no_state(message: Message, state: FSMContext) -> None:
    """Catch-all for messages without a matching handler.

    This covers users who send text before /start (state is None)
    and any other unhandled text in unexpected states.
    """
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Привет! Используйте /start чтобы начать работу с ботом."
        )
    else:
        await message.answer(
            "Не удалось обработать сообщение. Попробуйте /start."
        )


@router.callback_query()
async def fallback_callback(callback: CallbackQuery) -> None:
    """Catch-all for outdated/invalid callback queries."""
    await callback.answer(
        "Кнопка устарела. Используйте /start.", show_alert=True
    )
