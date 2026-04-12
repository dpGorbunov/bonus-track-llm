#!/usr/bin/env python3
"""
CLI-клиент для тестирования бота в терминале.
Эмулирует Telegram пользователя через dp.feed_update().

Использование:
    python scripts/cli_bot.py

Команды:
    /start, /help, /support, /rebuild, /profile - обычные команды
    @role:guest:student  - нажать inline-кнопку (callback_data)
    @profile:confirm     - подтвердить профиль
    @eval:<project_id>   - начать оценку проекта
    @score:0:4           - выставить 4 по критерию 0
    @score:confirm       - подтвердить оценку
    !state               - показать текущее FSM-состояние
    !data                - показать FSM data
    !quit                - выход
"""

import asyncio
import datetime
import logging
import os
import sys
from collections import deque
from pathlib import Path
from uuid import uuid4

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env defaults
os.environ.setdefault("BOT_TOKEN", "42:TEST")
os.environ.setdefault("REDIS_PASSWORD", os.environ.get("REDIS_PASSWORD", "testpassword"))

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, EditMessageText, AnswerCallbackQuery, DeleteMessage
from aiogram.methods.base import Response, TelegramType, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    ResponseParameters,
    Update,
    User,
    UNSET_PARSE_MODE,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("cli_bot")

# Colors
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

USER_ID = 777
CHAT_ID = 777
BOT_ID = 42


class CapturingSession(BaseSession):
    """Session that captures outgoing requests instead of sending to Telegram."""

    def __init__(self):
        super().__init__()
        self.captured: deque[TelegramMethod] = deque()

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout=UNSET_PARSE_MODE) -> TelegramType:
        self.captured.append(method)

        # Return appropriate mock response
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.captured) + 100,
                date=datetime.datetime.now(),
                text=method.text or "",
                chat=Chat(id=method.chat_id, type="private"),
            )
        elif isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id or 1,
                date=datetime.datetime.now(),
                text=method.text or "",
                chat=Chat(id=method.chat_id, type="private"),
            )
        elif isinstance(method, AnswerCallbackQuery):
            return True
        elif isinstance(method, DeleteMessage):
            return True
        else:
            return True

    async def close(self):
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        raise NotImplementedError


class CLIBot(Bot):
    """Bot that captures outgoing messages for CLI display."""

    def __init__(self):
        session = CapturingSession()
        super().__init__(token="42:TEST", session=session)
        self._me = User(id=BOT_ID, is_bot=True, first_name="EventAI", username="eventai_bot", language_code="ru")

    def drain_messages(self) -> list[TelegramMethod]:
        """Get all captured outgoing methods and clear the queue."""
        msgs = list(self.session.captured)
        self.session.captured.clear()
        return msgs


def make_message(text: str, msg_id: int) -> Update:
    return Update(
        update_id=msg_id,
        message=Message(
            message_id=msg_id,
            date=datetime.datetime.now(),
            text=text,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester", username="tester"),
        ),
    )


def make_callback(data: str, msg_id: int) -> Update:
    return Update(
        update_id=msg_id + 10000,
        callback_query=CallbackQuery(
            id=str(uuid4()),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester", username="tester"),
            chat_instance="cli",
            data=data,
            message=Message(
                message_id=msg_id,
                date=datetime.datetime.now(),
                text="[button]",
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=BOT_ID, is_bot=True, first_name="EventAI"),
            ),
        ),
    )


def display_response(method: TelegramMethod):
    """Pretty-print a bot response."""
    if isinstance(method, SendMessage):
        text = method.text or ""
        print(f"\n{GREEN}Bot:{RESET} {text}")

        # Show inline keyboard if present
        if method.reply_markup and hasattr(method.reply_markup, "inline_keyboard"):
            print(f"{DIM}  Кнопки:{RESET}")
            for row in method.reply_markup.inline_keyboard:
                for btn in row:
                    print(f"{DIM}    [{btn.text}] -> @{btn.callback_data}{RESET}")

    elif isinstance(method, EditMessageText):
        text = method.text or ""
        print(f"\n{GREEN}Bot (edit):{RESET} {text}")

        if method.reply_markup and hasattr(method.reply_markup, "inline_keyboard"):
            print(f"{DIM}  Кнопки:{RESET}")
            for row in method.reply_markup.inline_keyboard:
                for btn in row:
                    print(f"{DIM}    [{btn.text}] -> @{btn.callback_data}{RESET}")

    elif isinstance(method, AnswerCallbackQuery):
        if method.text:
            print(f"{YELLOW}  (popup) {method.text}{RESET}")


async def setup_dispatcher(bot: CLIBot) -> Dispatcher:
    """Set up dispatcher with all routers and middlewares."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from src.core.config import settings
    from src.services.platform_client import PlatformClient

    dp = Dispatcher(storage=MemoryStorage())

    # Real DB
    engine = create_async_engine(settings.database_url, pool_size=5)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Platform client - direct to OpenRouter for CLI testing
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        from pydantic import SecretStr
        platform = PlatformClient(
            platform_url="https://openrouter.ai/api",
            master_token="unused",
        )
        platform._token = SecretStr(openrouter_key)
        print(f"{DIM}LLM: OpenRouter ({settings.llm_model}){RESET}")
    else:
        platform = PlatformClient(
            platform_url=settings.platform_url,
            master_token=settings.master_token,
        )
        print(f"{YELLOW}WARNING: OPENROUTER_API_KEY not set, LLM calls will fail{RESET}")

    # DB session middleware (simplified for CLI)
    from aiogram import BaseMiddleware
    from typing import Any, Awaitable, Callable
    from aiogram.types import TelegramObject

    class CLIDbMiddleware(BaseMiddleware):
        async def __call__(self, handler: Callable, event: TelegramObject, data: dict[str, Any]) -> Any:
            async with session_factory() as session:
                data["db"] = session
                try:
                    result = await handler(event, data)
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise

    class CLIPlatformMiddleware(BaseMiddleware):
        async def __call__(self, handler: Callable, event: TelegramObject, data: dict[str, Any]) -> Any:
            data["platform"] = platform
            return await handler(event, data)

    # Register middlewares (no throttle for CLI)
    dp.message.middleware(CLIDbMiddleware())
    dp.message.middleware(CLIPlatformMiddleware())
    dp.callback_query.middleware(CLIDbMiddleware())
    dp.callback_query.middleware(CLIPlatformMiddleware())

    # Register routers (same order as main.py)
    from src.bot.routers.start import router as start_router
    from src.bot.routers.profiling import router as profiling_router
    from src.bot.routers.expert import router as expert_router
    from src.bot.routers.detail import router as detail_router
    from src.bot.routers.support import router as support_router
    from src.bot.routers.program import router as program_router

    dp.include_router(start_router)
    dp.include_router(profiling_router)
    dp.include_router(expert_router)
    dp.include_router(detail_router)
    dp.include_router(support_router)
    dp.include_router(program_router)

    # Fallback router MUST be last
    from src.bot.routers.fallback import router as fallback_router
    dp.include_router(fallback_router)

    return dp


async def main():
    print(f"\n{BOLD}EventAI Bot - CLI тестирование{RESET}")
    print(f"{DIM}Команды: /start, /help, /support, /rebuild, /profile{RESET}")
    print(f"{DIM}Кнопки:  @callback_data (например @role:guest:student){RESET}")
    print(f"{DIM}Инфо:    !state, !data, !quit{RESET}")
    print(f"{DIM}{'='*50}{RESET}\n")

    bot = CLIBot()
    dp = await setup_dispatcher(bot)
    await dp.emit_startup()

    msg_counter = 0

    try:
        while True:
            try:
                user_input = input(f"\n{CYAN}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            # Meta commands
            if user_input == "!quit":
                break

            if user_input == "!state":
                state = dp.fsm.get_context(bot, user_id=USER_ID, chat_id=CHAT_ID)
                current = await state.get_state()
                print(f"{DIM}  FSM state: {current or '(none)'}{RESET}")
                continue

            if user_input == "!data":
                state = dp.fsm.get_context(bot, user_id=USER_ID, chat_id=CHAT_ID)
                data = await state.get_data()
                # Truncate long values
                display_data = {}
                for k, v in data.items():
                    if isinstance(v, list) and len(v) > 3:
                        display_data[k] = f"[{len(v)} items]"
                    elif isinstance(v, str) and len(v) > 100:
                        display_data[k] = v[:100] + "..."
                    else:
                        display_data[k] = v
                import json
                print(f"{DIM}  FSM data: {json.dumps(display_data, indent=2, default=str, ensure_ascii=False)}{RESET}")
                continue

            msg_counter += 1

            # Callback (button press)
            if user_input.startswith("@"):
                callback_data = user_input[1:]
                update = make_callback(callback_data, msg_counter)
            else:
                update = make_message(user_input, msg_counter)

            # Feed to dispatcher
            try:
                await dp.feed_update(bot, update)
            except Exception as e:
                print(f"\n{YELLOW}Error: {e}{RESET}")
                logger.exception("Handler error")

            # Display captured responses
            messages = bot.drain_messages()
            for method in messages:
                display_response(method)

    finally:
        await dp.emit_shutdown()
        print(f"\n{DIM}Bye!{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
