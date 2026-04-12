"""
Comprehensive tests for all 6 bot routers using dp.feed_update() + MockedBot.

Tests FSM state transitions, callback handlers, message handlers,
and DB operations for: start, profiling, program, detail, support, expert.

Requires running PostgreSQL at localhost:5432 and Redis at localhost:6379.
PlatformClient is mocked (no real LLM calls).
"""

import os
import datetime
from datetime import date, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message as TgMessage, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Env must be set before src imports
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai",
)
os.environ.setdefault("REDIS_URL", "redis://:testpassword@localhost:6379/0")
os.environ.setdefault("REDIS_PASSWORD", "testpassword")

from tests.mocked_bot import MockedBot
from tests.conftest import make_callback, make_message

from src.bot.states import BotStates
from src.models.event import Event
from src.models.expert import Expert
from src.models.expert_score import ExpertScore
from src.models.guest_profile import GuestProfile
from src.models.project import Project
from src.models.recommendation import Recommendation
from src.models.room import Room
from src.models.schedule_slot import ScheduleSlot
from src.models.support_log import SupportLog
from src.models.user import User


# ---------------------------------------------------------------------------
# Test-only middlewares
# ---------------------------------------------------------------------------


class _DbMiddleware(BaseMiddleware):
    """Injects a pre-created AsyncSession into handler data."""

    def __init__(self):
        self.session: AsyncSession | None = None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["db"] = self.session
        return await handler(event, data)


class _PlatformMiddleware(BaseMiddleware):
    """Injects a mocked PlatformClient."""

    def __init__(self):
        self.platform: Any = None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["platform"] = self.platform
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Shared state: single Dispatcher with routers attached once
# ---------------------------------------------------------------------------

_dp: Dispatcher | None = None
_bot: MockedBot | None = None
_db_mw: _DbMiddleware | None = None
_platform_mw: _PlatformMiddleware | None = None


def _get_dp_and_bot() -> tuple[Dispatcher, MockedBot]:
    """Return singleton Dispatcher + Bot. Routers attached once.

    Each test uses unique user IDs, so MemoryStorage provides isolation
    without needing to reset the storage between tests.
    """
    global _dp, _bot, _db_mw, _platform_mw

    if _dp is not None:
        # Clear queued bot responses/requests from previous test
        _bot.session.responses.clear()
        _bot.session.requests.clear()
        return _dp, _bot

    storage = MemoryStorage()
    _dp = Dispatcher(storage=storage)
    _bot = MockedBot()

    _db_mw = _DbMiddleware()
    _platform_mw = _PlatformMiddleware()

    _dp.message.middleware(_db_mw)
    _dp.callback_query.middleware(_db_mw)
    _dp.message.middleware(_platform_mw)
    _dp.callback_query.middleware(_platform_mw)

    # Import routers fresh (they are module-level singletons)
    from src.bot.routers.start import router as start_router
    from src.bot.routers.profiling import router as profiling_router
    from src.bot.routers.expert import router as expert_router
    from src.bot.routers.detail import router as detail_router
    from src.bot.routers.support import router as support_router
    from src.bot.routers.program import router as program_router
    from src.bot.routers.fallback import router as fallback_router

    # Registration order matches main.py
    _dp.include_router(start_router)
    _dp.include_router(profiling_router)
    _dp.include_router(expert_router)
    _dp.include_router(detail_router)
    _dp.include_router(support_router)
    _dp.include_router(program_router)
    _dp.include_router(fallback_router)

    return _dp, _bot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def db():
    """Real PostgreSQL session with rollback after each test."""
    eng = create_async_engine(TEST_DB_URL, pool_size=2)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            yield session
            await session.rollback()
    await eng.dispose()


@pytest_asyncio.fixture
async def seed(db: AsyncSession):
    """Seed event + rooms + projects + schedule slots."""
    # Deactivate any pre-existing active events so tests see only the seed event
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Event).where(Event.is_active.is_(True)).values(is_active=False)
    )
    await db.flush()

    event = Event(
        id=uuid4(),
        name="Test Demo Day",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=1),
        description="Test event",
        is_active=True,
        evaluation_criteria=[
            "Техническая сложность",
            "Практическая применимость",
            "Инновационность",
        ],
    )
    db.add(event)

    room1 = Room(id=uuid4(), event_id=event.id, name="Зал NLP", display_order=1)
    room2 = Room(id=uuid4(), event_id=event.id, name="Зал CV", display_order=2)
    db.add_all([room1, room2])

    project_data = [
        (
            "ChatLaw",
            "Чат-бот для юридических консультаций на основе GPT и RAG",
            ["NLP", "LLM", "RAG"],
            ["Python", "LangChain", "FAISS"],
        ),
        (
            "MedVision",
            "AI-система для анализа медицинских снимков",
            ["CV", "медицина"],
            ["Python", "PyTorch", "MONAI"],
        ),
        (
            "SentimentScope",
            "Анализ тональности отзывов в реальном времени",
            ["NLP", "анализ тональности"],
            ["Python", "Transformers", "FastAPI"],
        ),
    ]
    projects = []
    tomorrow = datetime.datetime.now(timezone.utc) + timedelta(hours=24)
    slots = []
    for i, (title, desc, tags, stack) in enumerate(project_data):
        p = Project(
            id=uuid4(),
            event_id=event.id,
            title=title,
            description=desc,
            tags=tags,
            tech_stack=stack,
            author=f"Author {i + 1}",
            telegram_contact=f"@author{i + 1}",
        )
        projects.append(p)
        db.add(p)

        room = room1 if i < 2 else room2
        slot_time = tomorrow + timedelta(minutes=20 * i)
        slot = ScheduleSlot(
            id=uuid4(),
            event_id=event.id,
            room_id=room.id,
            project_id=p.id,
            start_time=slot_time,
            end_time=slot_time + timedelta(minutes=20),
            day_number=1,
        )
        slots.append(slot)
        db.add(slot)

    await db.flush()

    return {
        "event": event,
        "rooms": [room1, room2],
        "projects": projects,
        "slots": slots,
    }


def _make_platform_mock() -> MagicMock:
    """Create a mocked PlatformClient that returns canned LLM responses."""
    platform = MagicMock()
    platform.platform_url = "http://mock-platform"
    platform.token = "mock-token"
    platform.chat_completion = AsyncMock(
        return_value={
            "choices": [
                {
                    "message": {
                        "content": '{"action":"reply","message":"Расскажите подробнее."}',
                    }
                }
            ]
        }
    )
    platform.embedding = AsyncMock(return_value=[0.0] * 768)
    platform.close = AsyncMock()
    return platform


def _setup_dp(db_session: AsyncSession, platform: Any | None = None) -> tuple[Dispatcher, MockedBot]:
    """Get the singleton dispatcher, bind DB session and platform mock."""
    dp, bot = _get_dp_and_bot()

    if platform is None:
        platform = _make_platform_mock()

    _db_mw.session = db_session
    _platform_mw.platform = platform

    return dp, bot


def _queue_send(bot: MockedBot) -> None:
    """Queue a successful SendMessage response."""
    bot.add_result_for(
        SendMessage,
        ok=True,
        result=TgMessage(
            message_id=1,
            date=datetime.datetime.now(),
            chat=Chat(id=42, type="private"),
            text="ok",
        ),
    )


def _queue_cb(bot: MockedBot) -> None:
    """Queue a successful AnswerCallbackQuery response."""
    bot.add_result_for(AnswerCallbackQuery, ok=True, result=True)


def _queue_edit(bot: MockedBot) -> None:
    """Queue a successful EditMessageText response."""
    bot.add_result_for(
        EditMessageText,
        ok=True,
        result=TgMessage(
            message_id=1,
            date=datetime.datetime.now(),
            chat=Chat(id=42, type="private"),
            text="edited",
        ),
    )


async def _get_state(dp: Dispatcher, bot: MockedBot, user_id: int = 42) -> str | None:
    """Read current FSM state."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, user_id=user_id, chat_id=user_id)
    ctx = FSMContext(storage=dp.storage, key=key)
    return await ctx.get_state()


async def _get_data(dp: Dispatcher, bot: MockedBot, user_id: int = 42) -> dict:
    """Read FSM data."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, user_id=user_id, chat_id=user_id)
    ctx = FSMContext(storage=dp.storage, key=key)
    return await ctx.get_data()


async def _set_state(dp: Dispatcher, bot: MockedBot, state: str | None, user_id: int = 42) -> None:
    """Set FSM state."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, user_id=user_id, chat_id=user_id)
    ctx = FSMContext(storage=dp.storage, key=key)
    await ctx.set_state(state)


async def _set_data(dp: Dispatcher, bot: MockedBot, data: dict, user_id: int = 42) -> None:
    """Set FSM data."""
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, user_id=user_id, chat_id=user_id)
    ctx = FSMContext(storage=dp.storage, key=key)
    await ctx.update_data(**data)


# =========================================================================
# START ROUTER TESTS
# =========================================================================


class TestStartRouter:
    """Tests for /start command and role selection callbacks."""

    @pytest.mark.asyncio
    async def test_start_new_user_sets_choose_role(self, db: AsyncSession, seed):
        """New user /start -> choose_role state, role keyboard shown."""
        dp, bot = _setup_dp(db)
        uid = 9001

        _queue_send(bot)

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.choose_role.state

        result = await db.execute(select(User).where(User.telegram_user_id == str(uid)))
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.full_name == "TestUser"

    @pytest.mark.asyncio
    async def test_start_no_active_event(self, db: AsyncSession):
        """No active event -> error message, no state transition."""
        # Deactivate any pre-existing events so the test sees no active ones
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Event).where(Event.is_active.is_(True)).values(is_active=False)
        )
        await db.flush()

        dp, bot = _setup_dp(db)
        uid = 9002

        _queue_send(bot)

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "Нет активных мероприятий" in req.text

    @pytest.mark.asyncio
    async def test_start_returning_user_with_profile(self, db: AsyncSession, seed):
        """User with existing profile -> view_program."""
        uid = 9003
        user = User(
            telegram_user_id=str(uid),
            full_name="Returning User",
            username="returning",
        )
        db.add(user)
        await db.flush()

        profile = GuestProfile(
            user_id=user.id,
            event_id=seed["event"].id,
            selected_tags=["NLP"],
            keywords=["chatbot"],
        )
        db.add(profile)
        await db.flush()

        dp, bot = _setup_dp(db)

        _queue_send(bot)  # "С возвращением" or "Профиль найден"

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

    @pytest.mark.asyncio
    async def test_start_returning_expert(self, db: AsyncSession, seed):
        """User who is expert with bot_started -> expert_dashboard."""
        uid = 9004
        user = User(
            telegram_user_id=str(uid),
            full_name="Expert User",
            role_code="expert",
        )
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="test_returning_expert",
            name="Expert User",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        dp, bot = _setup_dp(db)

        _queue_send(bot)  # dashboard

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

    @pytest.mark.asyncio
    async def test_role_selection_guest_student(self, db: AsyncSession, seed):
        """role:guest:student callback -> onboard_nl_profile."""
        uid = 9005
        user = User(
            telegram_user_id=str(uid),
            full_name="Student",
            username="student",
        )
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.choose_role.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(user.id), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_cb(bot)    # callback.answer()
        _queue_edit(bot)  # message.edit_text()

        update = make_callback("role:guest:student", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one()
        assert u.role_code == "guest"
        assert u.subrole == "student"

    @pytest.mark.asyncio
    async def test_role_selection_business(self, db: AsyncSession, seed):
        """role:business callback -> onboard_nl_profile."""
        uid = 9006
        user = User(
            telegram_user_id=str(uid),
            full_name="Business",
            username="biz",
        )
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.choose_role.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(user.id), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_cb(bot)
        _queue_edit(bot)

        update = make_callback("role:business", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one()
        assert u.role_code == "business"
        assert u.subrole is None

    @pytest.mark.asyncio
    async def test_role_shortcut_shows_all_projects(self, db: AsyncSession, seed):
        """role:shortcut -> view_program, all projects listed."""
        uid = 9007
        user = User(telegram_user_id=str(uid), full_name="Shortcut User")
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.choose_role.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(user.id), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_cb(bot)    # callback.answer()
        _queue_edit(bot)  # "Загружаю все проекты..."
        _queue_send(bot)  # project list

        update = make_callback("role:shortcut", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

    @pytest.mark.asyncio
    async def test_start_expert_deep_link(self, db: AsyncSession, seed):
        """Expert deep link /start expert_<code> -> expert_dashboard."""
        uid = 9008
        # Expert needs a placeholder user_id (NOT NULL constraint)
        placeholder = User(telegram_user_id="placeholder_exp", full_name="Placeholder")
        db.add(placeholder)
        await db.flush()

        expert = Expert(
            user_id=placeholder.id,
            event_id=seed["event"].id,
            invite_code="deep_link_test",
            name="Invited Expert",
            room_id=seed["rooms"][0].id,
            bot_started=False,
        )
        db.add(expert)
        await db.flush()

        dp, bot = _setup_dp(db)

        _queue_send(bot)  # dashboard

        update = make_message("/start expert_deep_link_test", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        result = await db.execute(select(Expert).where(Expert.invite_code == "deep_link_test"))
        exp = result.scalar_one()
        assert exp.bot_started is True

    @pytest.mark.asyncio
    async def test_start_invalid_expert_link(self, db: AsyncSession, seed):
        """Invalid expert deep link -> choose_role fallback."""
        uid = 9009
        dp, bot = _setup_dp(db)

        _queue_send(bot)  # "Приглашение недействительно."
        _queue_send(bot)  # "Выберите роль:"

        update = make_message("/start expert_nonexistent", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.choose_role.state


# =========================================================================
# PROFILING ROUTER TESTS
# =========================================================================


class TestProfilingRouter:
    """Tests for NL profiling and profile confirmation."""

    @pytest.mark.asyncio
    async def test_nl_profile_reply_stays_in_state(self, db: AsyncSession, seed):
        """LLM returns action=reply -> stays in onboard_nl_profile."""
        uid = 9010
        user = User(
            telegram_user_id=str(uid),
            full_name="Profiling User",
            role_code="guest",
            subrole="student",
        )
        db.add(user)
        await db.flush()

        platform = _make_platform_mock()
        platform.chat_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": '{"action":"reply","message":"Какие области AI?"}'}}]
            }
        )

        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.onboard_nl_profile.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "nl_conversation": [],
            "nl_turn": 0,
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("Мне интересны чат-боты", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

        data = await _get_data(dp, bot, user_id=uid)
        assert data["nl_turn"] == 1
        assert len(data["nl_conversation"]) == 2

    @pytest.mark.asyncio
    async def test_nl_profile_extracts_profile(self, db: AsyncSession, seed):
        """LLM returns action=profile -> transition to onboard_confirm."""
        uid = 9011
        user = User(
            telegram_user_id=str(uid),
            full_name="Profile Extract",
            role_code="guest",
            subrole="student",
        )
        db.add(user)
        await db.flush()

        platform = _make_platform_mock()
        platform.chat_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": '{"action":"profile","interests":["NLP","LLM"],"goals":["изучить RAG"],"summary":"Студент, NLP"}'}}]
            }
        )

        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.onboard_nl_profile.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "nl_conversation": [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Расскажите подробнее"},
            ],
            "nl_turn": 1,
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("Мне нравятся NLP и RAG", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_confirm.state

        data = await _get_data(dp, bot, user_id=uid)
        assert data["extracted_profile"]["interests"] == ["NLP", "LLM"]

    @pytest.mark.asyncio
    async def test_profile_forced_reply_on_first_turn(self, db: AsyncSession, seed):
        """If LLM says profile on first turn with no assistant history, force reply."""
        uid = 9012
        user = User(
            telegram_user_id=str(uid),
            full_name="First Turn",
            role_code="guest",
            subrole="student",
        )
        db.add(user)
        await db.flush()

        platform = _make_platform_mock()
        platform.chat_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": '{"action":"profile","interests":["AI"],"goals":["learn"],"summary":"test"}'}}]
            }
        )

        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.onboard_nl_profile.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "nl_conversation": [],
            "nl_turn": 0,
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("AI", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

    @pytest.mark.asyncio
    async def test_profile_confirm_creates_guest_profile(self, db: AsyncSession, seed):
        """profile:confirm -> GuestProfile created, view_program."""
        uid = 9013
        user = User(
            telegram_user_id=str(uid),
            full_name="Confirm User",
            role_code="guest",
            subrole="student",
        )
        db.add(user)
        await db.flush()

        platform = _make_platform_mock()
        platform.embedding = AsyncMock(return_value=[0.1] * 768)

        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.onboard_confirm.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "extracted_profile": {
                "interests": ["NLP", "LLM"],
                "goals": ["изучить RAG"],
                "summary": "Студент",
                "company": None,
                "position": None,
                "business_objectives": None,
                "raw_text": "NLP и RAG",
            },
        }, user_id=uid)

        _queue_cb(bot)    # callback.answer()
        _queue_edit(bot)  # "Профиль сохранен..."
        _queue_send(bot)  # program or fallback

        # Mock generate_recommendations to avoid raw SQL issues in test DB
        with patch("src.bot.routers.profiling.generate_recommendations", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = []  # No recommendations -> fallback message

            update = make_callback("profile:confirm", user_id=uid, chat_id=uid)
            await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

        result = await db.execute(select(GuestProfile).where(GuestProfile.user_id == user.id))
        profile = result.scalar_one_or_none()
        assert profile is not None
        assert profile.selected_tags == ["NLP", "LLM"]

    @pytest.mark.asyncio
    async def test_profile_retry_resets_conversation(self, db: AsyncSession, seed):
        """profile:retry -> back to onboard_nl_profile, conversation cleared."""
        uid = 9014
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.onboard_confirm.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(uuid4()),
            "event_id": str(seed["event"].id),
            "nl_conversation": [{"role": "user", "content": "test"}],
            "nl_turn": 2,
            "extracted_profile": {"interests": ["AI"]},
        }, user_id=uid)

        _queue_cb(bot)
        _queue_edit(bot)

        update = make_callback("profile:retry", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

        data = await _get_data(dp, bot, user_id=uid)
        assert data["nl_conversation"] == []
        assert data["nl_turn"] == 0


# =========================================================================
# PROGRAM ROUTER TESTS
# =========================================================================


class TestProgramRouter:
    """Tests for view_program state handlers."""

    @pytest.mark.asyncio
    async def test_rebuild_command(self, db: AsyncSession, seed):
        """/rebuild -> onboard_nl_profile."""
        uid = 9020
        user = User(telegram_user_id=str(uid), full_name="Rebuild User", role_code="guest")
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(uuid4()),
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("/rebuild", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

    @pytest.mark.asyncio
    async def test_support_command(self, db: AsyncSession, seed):
        """/support -> support_chat state."""
        uid = 9021
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(uuid4()), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_send(bot)

        update = make_message("/support", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.support_chat.state

    @pytest.mark.asyncio
    async def test_profile_command_shows_profile(self, db: AsyncSession, seed):
        """/profile -> shows profile text."""
        uid = 9022
        user = User(telegram_user_id=str(uid), full_name="Profile Viewer", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(
            user_id=user.id,
            event_id=seed["event"].id,
            selected_tags=["NLP", "LLM"],
            keywords=["chatbot"],
            nl_summary="NLP enthusiast",
        )
        db.add(profile)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("/profile", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "NLP" in req.text

    @pytest.mark.asyncio
    async def test_profile_command_no_profile(self, db: AsyncSession, seed):
        """/profile without profile_id -> error message."""
        uid = 9023
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(uuid4()), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_send(bot)

        update = make_message("/profile", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "не найден" in req.text.lower()

    @pytest.mark.asyncio
    async def test_cb_profile_button(self, db: AsyncSession, seed):
        """cmd:profile callback -> shows profile."""
        uid = 9024
        user = User(telegram_user_id=str(uid), full_name="CB Profile", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id, selected_tags=["CV"])
        db.add(profile)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("cmd:profile", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "CV" in req.text

    @pytest.mark.asyncio
    async def test_cb_if_time_no_recs(self, db: AsyncSession, seed):
        """cmd:if_time without if_time recs -> 'Нет дополнительных'."""
        uid = 9025
        user = User(telegram_user_id=str(uid), full_name="IfTime User", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id)
        db.add(profile)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("cmd:if_time", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "Нет дополнительных" in req.text

    @pytest.mark.asyncio
    async def test_view_program_text_agent(self, db: AsyncSession, seed):
        """Free text in view_program -> PydanticAI agent runs (mocked)."""
        uid = 9026
        user = User(telegram_user_id=str(uid), full_name="Agent User", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id, selected_tags=["NLP"])
        db.add(profile)
        await db.flush()

        platform = _make_platform_mock()
        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
        }, user_id=uid)

        _queue_send(bot)

        with patch("src.bot.routers.program.create_agent") as mock_create:
            mock_agent = AsyncMock()
            mock_result = MagicMock()
            mock_result.output = "Вот ваши рекомендации по NLP проектам."
            mock_agent.run = AsyncMock(return_value=mock_result)
            mock_create.return_value = mock_agent

            update = make_message("Расскажи про NLP проекты", user_id=uid, chat_id=uid)
            await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "NLP" in req.text


# =========================================================================
# DETAIL ROUTER TESTS
# =========================================================================


class TestDetailRouter:
    """Tests for view_detail state handlers."""

    @pytest.mark.asyncio
    async def test_back_to_program(self, db: AsyncSession, seed):
        """cmd:back callback in view_detail -> view_program."""
        uid = 9030
        user = User(telegram_user_id=str(uid), full_name="Detail User", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id)
        db.add(profile)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_detail.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
            "current_project_id": str(seed["projects"][0].id),
            "current_project_rank": 1,
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)  # "Назад к программе."

        update = make_callback("cmd:back", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

    @pytest.mark.asyncio
    async def test_back_to_program_with_recs(self, db: AsyncSession, seed):
        """cmd:back with recommendations -> shows program with project names."""
        uid = 9031
        user = User(telegram_user_id=str(uid), full_name="Detail Recs", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id)
        db.add(profile)
        await db.flush()

        rec = Recommendation(
            profile_id=profile.id,
            project_id=seed["projects"][0].id,
            relevance_score=90.0,
            category="must_see",
            rank=1,
            slot_id=seed["slots"][0].id,
        )
        db.add(rec)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_detail.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
            "current_project_id": str(seed["projects"][0].id),
            "current_project_rank": 1,
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("cmd:back", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "ChatLaw" in req.text

    @pytest.mark.asyncio
    async def test_generate_questions(self, db: AsyncSession, seed):
        """questions:<rank> -> generates Q&A via LLM."""
        uid = 9032
        user = User(
            telegram_user_id=str(uid),
            full_name="QA User",
            role_code="guest",
            subrole="student",
        )
        db.add(user)
        await db.flush()

        profile = GuestProfile(
            user_id=user.id,
            event_id=seed["event"].id,
            selected_tags=["NLP"],
        )
        db.add(profile)
        await db.flush()

        platform = _make_platform_mock()
        platform.chat_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": '{"questions":["Как работает RAG?","Какие данные?","Какая точность?"]}'}}]
            }
        )

        dp, bot = _setup_dp(db, platform)

        await _set_state(dp, bot, BotStates.view_detail.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
            "current_project_id": str(seed["projects"][0].id),
            "current_project_rank": 1,
        }, user_id=uid)

        _queue_cb(bot)    # callback.answer("Генерирую...")
        _queue_send(bot)  # questions

        update = make_callback("questions:1", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "Вопросы для проекта" in req.text

    @pytest.mark.asyncio
    async def test_detail_text_hint(self, db: AsyncSession, seed):
        """Text in view_detail -> hint to use buttons."""
        uid = 9033
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_detail.state, user_id=uid)
        await _set_data(dp, bot, {"current_project_rank": 1}, user_id=uid)

        _queue_send(bot)

        update = make_message("Привет", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "Используйте кнопки" in req.text


# =========================================================================
# SUPPORT ROUTER TESTS
# =========================================================================


class TestSupportRouter:
    """Tests for support_chat state handlers."""

    @pytest.mark.asyncio
    async def test_support_start_from_program(self, db: AsyncSession, seed):
        """support:start callback -> support_chat state."""
        uid = 9040
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.view_program.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(uuid4()), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("support:start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.support_chat.state

    @pytest.mark.asyncio
    async def test_support_text_forwards_message(self, db: AsyncSession, seed):
        """Text in support_chat -> creates SupportLog, sends confirmation."""
        uid = 9041
        user = User(telegram_user_id=str(uid), full_name="Support Sender", username="sender")
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.support_chat.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "support_timestamps": [],
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("Где найти расписание?", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "Сообщение отправлено" in req.text

        result = await db.execute(select(SupportLog).where(SupportLog.user_id == user.id))
        log = result.scalar_one_or_none()
        assert log is not None
        assert log.question == "Где найти расписание?"

    @pytest.mark.asyncio
    async def test_support_rate_limit(self, db: AsyncSession, seed):
        """After 3 messages in 5 minutes, rate limit kicks in."""
        import time

        uid = 9042
        user = User(telegram_user_id=str(uid), full_name="Rate Limited")
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        now = time.time()
        await _set_state(dp, bot, BotStates.support_chat.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "support_timestamps": [now - 10, now - 5, now - 1],
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("4th message", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "Лимит" in req.text

    @pytest.mark.asyncio
    async def test_support_back_to_program(self, db: AsyncSession, seed):
        """support:back callback -> view_program."""
        uid = 9043
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.support_chat.state, user_id=uid)
        await _set_data(dp, bot, {"user_id": str(uuid4()), "event_id": str(seed["event"].id)}, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("support:back", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

    @pytest.mark.asyncio
    async def test_support_back_with_recommendations(self, db: AsyncSession, seed):
        """support:back with recs -> shows program."""
        uid = 9044
        user = User(telegram_user_id=str(uid), full_name="Sup Back Recs", role_code="guest")
        db.add(user)
        await db.flush()

        profile = GuestProfile(user_id=user.id, event_id=seed["event"].id)
        db.add(profile)
        await db.flush()

        rec = Recommendation(
            profile_id=profile.id,
            project_id=seed["projects"][0].id,
            relevance_score=85.0,
            category="must_see",
            rank=1,
        )
        db.add(rec)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.support_chat.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "profile_id": str(profile.id),
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)

        update = make_callback("support:back", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.view_program.state

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "ChatLaw" in req.text

    @pytest.mark.asyncio
    async def test_support_session_lost(self, db: AsyncSession, seed):
        """Text with missing user_id -> 'Сессия потеряна'."""
        uid = 9045
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.support_chat.state, user_id=uid)
        # No user_id in state data

        _queue_send(bot)

        update = make_message("test", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "Сессия потеряна" in req.text


# =========================================================================
# EXPERT ROUTER TESTS
# =========================================================================


class TestExpertRouter:
    """Tests for expert_dashboard and expert_evaluation states."""

    @pytest.mark.asyncio
    async def test_expert_dashboard_display(self, db: AsyncSession, seed):
        """Expert with room -> dashboard shown."""
        uid = 9050
        user = User(telegram_user_id=str(uid), full_name="Expert Dash", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="dash_test_001",
            name="Expert Dash",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        dp, bot = _setup_dp(db)

        _queue_send(bot)  # dashboard

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        req = bot.get_request()
        assert "Expert Dash" in req.text
        assert "Прогресс" in req.text

    @pytest.mark.asyncio
    async def test_start_evaluation(self, db: AsyncSession, seed):
        """eval:<project_id> callback -> expert_evaluation."""
        uid = 9051
        user = User(telegram_user_id=str(uid), full_name="Evaluator", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="eval_test_001",
            name="Evaluator",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        project = seed["projects"][0]

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_dashboard.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(expert.id),
            "criteria": ["Техническая сложность", "Практическая применимость", "Инновационность"],
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)  # first criterion

        update = make_callback(f"eval:{project.id}", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_evaluation.state

        bot.get_request()  # AnswerCallbackQuery
        req = bot.get_request()
        assert "Критерий 1/" in req.text

    @pytest.mark.asyncio
    async def test_score_criterion_progression(self, db: AsyncSession, seed):
        """Scoring criteria -> next criterion shown."""
        uid = 9052
        user = User(telegram_user_id=str(uid), full_name="Scorer", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="score_test_001",
            name="Scorer",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        project = seed["projects"][0]
        criteria = ["Техническая сложность", "Практическая применимость", "Инновационность"]

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(expert.id),
            "criteria": criteria,
            "eval_project_id": str(project.id),
            "eval_project_title": project.title,
            "eval_scores": {},
            "eval_criterion_index": 0,
            "eval_awaiting_comment": False,
        }, user_id=uid)

        _queue_cb(bot)
        _queue_edit(bot)

        update = make_callback("score:0:4", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        data = await _get_data(dp, bot, user_id=uid)
        assert data["eval_scores"]["Техническая сложность"] == 4
        assert data["eval_criterion_index"] == 1

    @pytest.mark.asyncio
    async def test_score_all_criteria_asks_comment(self, db: AsyncSession, seed):
        """After scoring all criteria -> ask for comment."""
        uid = 9053
        user = User(telegram_user_id=str(uid), full_name="All Criteria", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="all_crit_001",
            name="All Criteria",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        project = seed["projects"][0]
        criteria = ["Техническая сложность", "Практическая применимость", "Инновационность"]

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(expert.id),
            "criteria": criteria,
            "eval_project_id": str(project.id),
            "eval_project_title": project.title,
            "eval_scores": {
                "Техническая сложность": 4,
                "Практическая применимость": 5,
            },
            "eval_criterion_index": 2,
            "eval_awaiting_comment": False,
        }, user_id=uid)

        _queue_cb(bot)
        _queue_edit(bot)

        update = make_callback("score:2:3", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        data = await _get_data(dp, bot, user_id=uid)
        assert data["eval_awaiting_comment"] is True
        assert data["eval_scores"]["Инновационность"] == 3

    @pytest.mark.asyncio
    async def test_comment_input_shows_confirm(self, db: AsyncSession, seed):
        """Text comment -> shows final summary."""
        uid = 9054
        user = User(telegram_user_id=str(uid), full_name="Commenter", role_code="expert")
        db.add(user)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(uuid4()),
            "eval_project_id": str(uuid4()),
            "eval_project_title": "TestProject",
            "eval_scores": {"Техника": 4, "Инновации": 5},
            "eval_criterion_index": 2,
            "eval_awaiting_comment": True,
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("Хороший проект, рекомендую", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        data = await _get_data(dp, bot, user_id=uid)
        assert data["eval_comment"] == "Хороший проект, рекомендую"
        assert data["eval_awaiting_comment"] is False

        req = bot.get_request()
        assert "Итоговая оценка" in req.text

    @pytest.mark.asyncio
    async def test_comment_skip_with_dash(self, db: AsyncSession, seed):
        """Comment '-' -> no comment."""
        uid = 9055
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "eval_project_title": "SkipCommentProject",
            "eval_scores": {"A": 3},
            "eval_awaiting_comment": True,
            "expert_id": str(uuid4()),
        }, user_id=uid)

        _queue_send(bot)

        update = make_message("-", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        data = await _get_data(dp, bot, user_id=uid)
        assert data["eval_comment"] is None

    @pytest.mark.asyncio
    async def test_confirm_score_saves_to_db(self, db: AsyncSession, seed):
        """score:confirm -> ExpertScore saved to DB, back to dashboard."""
        uid = 9056
        user = User(telegram_user_id=str(uid), full_name="Confirmer", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="confirm_score_001",
            name="Confirmer",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        project = seed["projects"][0]

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(expert.id),
            "criteria": ["Техническая сложность", "Инновационность"],
            "eval_project_id": str(project.id),
            "eval_project_title": project.title,
            "eval_scores": {
                "Техническая сложность": 4,
                "Инновационность": 5,
            },
            "eval_comment": "Excellent work",
            "eval_awaiting_comment": False,
        }, user_id=uid)

        _queue_cb(bot)    # callback.answer()
        _queue_send(bot)  # "Оценка сохранена."
        _queue_send(bot)  # dashboard

        update = make_callback("score:confirm", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        result = await db.execute(
            select(ExpertScore).where(
                ExpertScore.expert_id == expert.id,
                ExpertScore.project_id == project.id,
            )
        )
        score = result.scalar_one_or_none()
        assert score is not None
        assert score.criteria_scores["Техническая сложность"] == 4
        assert score.criteria_scores["Инновационность"] == 5
        assert score.comment == "Excellent work"

    @pytest.mark.asyncio
    async def test_cancel_score_back_to_dashboard(self, db: AsyncSession, seed):
        """score:cancel -> discard scores, back to dashboard."""
        uid = 9057
        user = User(telegram_user_id=str(uid), full_name="Canceller", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="cancel_score_001",
            name="Canceller",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {
            "user_id": str(user.id),
            "event_id": str(seed["event"].id),
            "expert_id": str(expert.id),
            "criteria": ["A", "B"],
            "eval_project_id": str(seed["projects"][0].id),
            "eval_project_title": "X",
            "eval_scores": {"A": 3},
            "eval_awaiting_comment": False,
        }, user_id=uid)

        _queue_cb(bot)
        _queue_send(bot)  # "Оценка отменена."
        _queue_send(bot)  # dashboard

        update = make_callback("score:cancel", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        result = await db.execute(select(ExpertScore).where(ExpertScore.expert_id == expert.id))
        scores = list(result.scalars().all())
        assert len(scores) == 0

    @pytest.mark.asyncio
    async def test_text_without_comment_hint(self, db: AsyncSession, seed):
        """Text when not awaiting comment -> hint to use buttons."""
        uid = 9058
        dp, bot = _setup_dp(db)

        await _set_state(dp, bot, BotStates.expert_evaluation.state, user_id=uid)
        await _set_data(dp, bot, {"eval_awaiting_comment": False}, user_id=uid)

        _queue_send(bot)

        update = make_message("random text", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "кнопки" in req.text.lower()

    @pytest.mark.asyncio
    async def test_expert_no_room_assigned(self, db: AsyncSession, seed):
        """Expert without room -> message about no room."""
        uid = 9059
        user = User(telegram_user_id=str(uid), full_name="No Room Expert", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="no_room_001",
            name="No Room Expert",
            room_id=None,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        dp, bot = _setup_dp(db)

        _queue_send(bot)

        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        req = bot.get_request()
        assert "не назначен зал" in req.text


# =========================================================================
# CROSS-ROUTER FLOW TESTS
# =========================================================================


class TestCrossRouterFlows:
    """Tests that span multiple routers."""

    @pytest.mark.asyncio
    async def test_full_guest_flow_start_to_profiling(self, db: AsyncSession, seed):
        """New user: /start -> choose_role -> role selection -> onboard_nl_profile."""
        uid = 9060
        dp, bot = _setup_dp(db)

        # Step 1: /start -> choose_role
        _queue_send(bot)
        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.choose_role.state

        # Step 2: role:guest:student -> onboard_nl_profile
        _queue_cb(bot)
        _queue_edit(bot)
        update = make_callback("role:guest:student", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.onboard_nl_profile.state

    @pytest.mark.asyncio
    async def test_expert_full_evaluation_flow(self, db: AsyncSession, seed):
        """Expert: dashboard -> eval -> score all -> comment -> confirm -> dashboard."""
        uid = 9061
        user = User(telegram_user_id=str(uid), full_name="Full Eval Expert", role_code="expert")
        db.add(user)
        await db.flush()

        expert = Expert(
            user_id=user.id,
            event_id=seed["event"].id,
            invite_code="full_eval_001",
            name="Full Eval Expert",
            room_id=seed["rooms"][0].id,
            bot_started=True,
        )
        db.add(expert)
        await db.flush()

        project = seed["projects"][0]

        dp, bot = _setup_dp(db)

        # Step 1: /start -> expert_dashboard
        _queue_send(bot)
        update = make_message("/start", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        # Step 2: eval:<project> -> expert_evaluation
        _queue_cb(bot)
        _queue_send(bot)
        update = make_callback(f"eval:{project.id}", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_evaluation.state

        # Step 3: Score all 3 criteria
        for i in range(3):
            _queue_cb(bot)
            _queue_edit(bot)
            update = make_callback(f"score:{i}:{4 + (i % 2)}", user_id=uid, chat_id=uid)
            await dp.feed_update(bot, update)

        # Step 4: Comment
        _queue_send(bot)
        update = make_message("Good project", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        # Step 5: Confirm
        _queue_cb(bot)
        _queue_send(bot)  # "Оценка сохранена."
        _queue_send(bot)  # dashboard

        update = make_callback("score:confirm", user_id=uid, chat_id=uid)
        await dp.feed_update(bot, update)

        state = await _get_state(dp, bot, user_id=uid)
        assert state == BotStates.expert_dashboard.state

        result = await db.execute(
            select(ExpertScore).where(
                ExpertScore.expert_id == expert.id,
                ExpertScore.project_id == project.id,
            )
        )
        score = result.scalar_one_or_none()
        assert score is not None
        assert score.comment == "Good project"
        assert len(score.criteria_scores) == 3
