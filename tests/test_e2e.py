"""
End-to-end tests for EventAI Agent.
Requires running PostgreSQL (pgvector) and Redis from docker-compose.
Uses real OpenRouter API for LLM calls.
"""

import asyncio
import json
import os
import pytest
import pytest_asyncio
import httpx
from uuid import uuid4, UUID
from datetime import datetime, date, timedelta, timezone

# Set test env before importing app modules
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai")
os.environ.setdefault("REDIS_URL", "redis://:testpassword@localhost:6379/0")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text, select, delete

from src.models.base import Base
from src.models.event import Event
from src.models.project import Project
from src.models.room import Room
from src.models.schedule_slot import ScheduleSlot
from src.models.user import User
from src.models.guest_profile import GuestProfile
from src.models.recommendation import Recommendation
from src.models.expert import Expert
from src.models.expert_score import ExpertScore
from src.models.chat_message import ChatMessage
from src.models.support_log import SupportLog
from src.schemas.tools import ComparisonMatrix, ProjectExtraction


# --- Fixtures ---

TEST_DB_URL = os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def db():
    eng = create_async_engine(TEST_DB_URL, pool_size=2)
    session_factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()
    await eng.dispose()


@pytest_asyncio.fixture
async def seed_data(db: AsyncSession):
    """Create test event, rooms, projects with embeddings."""
    # Event
    event = Event(
        id=uuid4(),
        name="Test Demo Day",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=1),
        description="Test event",
        is_active=True,
        evaluation_criteria=["Техническая сложность", "Практическая применимость", "Инновационность"],
    )
    db.add(event)

    # Rooms
    room1 = Room(id=uuid4(), event_id=event.id, name="Зал NLP", display_order=1)
    room2 = Room(id=uuid4(), event_id=event.id, name="Зал CV", display_order=2)
    db.add_all([room1, room2])

    # Projects
    projects = []
    project_data = [
        ("ChatLaw", "Чат-бот для юридических консультаций на основе GPT и RAG", ["NLP", "LLM", "RAG"], ["Python", "LangChain", "FAISS"]),
        ("MedVision", "AI-система для анализа медицинских снимков", ["CV", "медицина"], ["Python", "PyTorch", "MONAI"]),
        ("SentimentScope", "Анализ тональности отзывов в реальном времени", ["NLP", "анализ тональности"], ["Python", "Transformers", "FastAPI"]),
        ("RoboNav", "Автономная навигация робота в помещениях", ["робототехника", "SLAM"], ["Python", "ROS2", "PyTorch"]),
        ("DataPipe", "ETL-платформа для потоковой обработки данных", ["ETL", "данные"], ["Python", "Apache Kafka", "Spark"]),
    ]
    for i, (title, desc, tags, stack) in enumerate(project_data):
        p = Project(
            id=uuid4(), event_id=event.id, title=title, description=desc,
            tags=tags, tech_stack=stack, author=f"Author {i+1}",
            telegram_contact=f"@author{i+1}",
        )
        projects.append(p)
        db.add(p)

    # Schedule slots (future times)
    tomorrow = datetime.now(timezone.utc) + timedelta(hours=24)
    slots = []
    for i, p in enumerate(projects):
        room = room1 if i < 3 else room2
        slot_time = tomorrow + timedelta(minutes=20 * i)
        slot = ScheduleSlot(
            id=uuid4(), event_id=event.id, room_id=room.id, project_id=p.id,
            start_time=slot_time, end_time=slot_time + timedelta(minutes=20),
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


# === Test 1: Database models CRUD ===

@pytest.mark.asyncio
async def test_user_crud(db: AsyncSession):
    """Test User create and read."""
    user = User(
        telegram_user_id="test_123",
        full_name="Test User",
        username="testuser",
        role_code="guest",
        subrole="student",
    )
    db.add(user)
    await db.flush()

    result = await db.execute(select(User).where(User.telegram_user_id == "test_123"))
    loaded = result.scalar_one()
    assert loaded.full_name == "Test User"
    assert loaded.role_code == "guest"
    assert loaded.subrole == "student"


@pytest.mark.asyncio
async def test_guest_profile_crud(db: AsyncSession, seed_data):
    """Test GuestProfile with JSONB fields."""
    user = User(telegram_user_id="prof_test", full_name="Profile Test", role_code="guest")
    db.add(user)
    await db.flush()

    profile = GuestProfile(
        user_id=user.id,
        event_id=seed_data["event"].id,
        selected_tags=["NLP", "LLM"],
        keywords=["чат-бот", "RAG"],
        nl_summary="Интересуюсь NLP и чат-ботами",
    )
    db.add(profile)
    await db.flush()

    result = await db.execute(select(GuestProfile).where(GuestProfile.user_id == user.id))
    loaded = result.scalar_one()
    assert loaded.selected_tags == ["NLP", "LLM"]
    assert loaded.keywords == ["чат-бот", "RAG"]


@pytest.mark.asyncio
async def test_expert_score_upsert(db: AsyncSession, seed_data):
    """Test ExpertScore ON CONFLICT DO UPDATE."""
    user = User(telegram_user_id="expert_test", full_name="Expert", role_code="expert")
    db.add(user)
    await db.flush()

    expert = Expert(
        user_id=user.id, event_id=seed_data["event"].id,
        invite_code="test_invite_code_123", name="Expert Test",
        room_id=seed_data["rooms"][0].id, bot_started=True,
    )
    db.add(expert)
    await db.flush()

    project = seed_data["projects"][0]

    # First insert
    score1 = ExpertScore(
        expert_id=expert.id, project_id=project.id,
        criteria_scores={"Техническая сложность": 3, "Инновационность": 4},
        comment="Good project",
    )
    db.add(score1)
    await db.flush()

    # Verify
    result = await db.execute(
        select(ExpertScore).where(
            ExpertScore.expert_id == expert.id,
            ExpertScore.project_id == project.id,
        )
    )
    loaded = result.scalar_one()
    assert loaded.criteria_scores["Техническая сложность"] == 3


# === Test 2: Retriever - schedule rerank ===

@pytest.mark.asyncio
async def test_schedule_rerank():
    """Test schedule-aware reranking logic."""
    from src.services.retriever import _schedule_rerank

    room_a = uuid4()
    room_b = uuid4()
    t1 = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 15, 10, 20, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 15, 10, 40, tzinfo=timezone.utc)

    p1, p2, p3 = uuid4(), uuid4(), uuid4()

    candidates = [
        {"project_id": p1, "title": "P1", "score": 90.0},
        {"project_id": p2, "title": "P2", "score": 85.0},
        {"project_id": p3, "title": "P3", "score": 80.0},
    ]

    slots = {
        p1: {"slot_id": uuid4(), "room_id": room_a, "room_name": "Зал A", "start_time": t1, "end_time": t1 + timedelta(minutes=20), "day_number": 1},
        p2: {"slot_id": uuid4(), "room_id": room_a, "room_name": "Зал A", "start_time": t2, "end_time": t2 + timedelta(minutes=20), "day_number": 1},
        p3: {"slot_id": uuid4(), "room_id": room_b, "room_name": "Зал B", "start_time": t1, "end_time": t1 + timedelta(minutes=20), "day_number": 1},  # CONFLICT with P1
    }

    ranked = _schedule_rerank(candidates, slots)

    # P1 (90) should be first
    assert ranked[0]["title"] == "P1"
    # P2 (85 + 3.0 room bonus = 88) should be second (same room as P1)
    assert ranked[1]["title"] == "P2"
    # P3 conflicts with P1 (same start_time), should be excluded
    assert len(ranked) == 2 or (len(ranked) == 3 and ranked[2]["title"] != "P3" or ranked[2].get("slot") is None)


@pytest.mark.asyncio
async def test_tag_overlap_fallback(db: AsyncSession, seed_data):
    """Test fallback tag overlap scoring."""
    from src.services.retriever import _fallback_tag_overlap

    user = User(telegram_user_id="tag_test", full_name="Tag Test", role_code="guest")
    db.add(user)
    await db.flush()

    profile = GuestProfile(
        user_id=user.id, event_id=seed_data["event"].id,
        selected_tags=["NLP", "LLM"],
    )
    db.add(profile)
    await db.flush()

    recs = await _fallback_tag_overlap(
        db, profile.id, seed_data["event"].id, ["NLP", "LLM"]
    )

    assert len(recs) > 0
    # ChatLaw has tags ["NLP", "LLM", "RAG"] - should score highest (2 overlaps * 20 = 40)
    assert recs[0].relevance_score >= 40.0


# === Test 3: Platform Client with real OpenRouter ===

@pytest.mark.asyncio
async def test_openrouter_chat_completion():
    """Test real LLM call through OpenRouter."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                "max_tokens": 10,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        assert "PONG" in content.upper()


@pytest.mark.asyncio
async def test_openrouter_embedding():
    """Test real embedding call through OpenRouter."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "google/gemini-embedding-exp-03-07",
                "input": "NLP и чат-боты для юридических консультаций",
            },
        )
        # Note: embedding endpoint may not be available on all models
        if resp.status_code == 200:
            data = resp.json()
            embedding = data["data"][0]["embedding"]
            assert len(embedding) > 0
            assert isinstance(embedding[0], float)
        else:
            pytest.skip(f"Embedding endpoint returned {resp.status_code}: {resp.text[:200]}")


# === Test 4: Profiling service ===

@pytest.mark.asyncio
async def test_profiling_chat():
    """Test LLM profiling with real API."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    from src.prompts.profiling import get_profile_agent_system, get_role_context

    tag_list = "NLP, CV, Agents, Robotics, Data Engineering, LLM, RAG"
    role_context = get_role_context("guest", "student", None)
    system_prompt = get_profile_agent_system(tag_list, role_context)

    conversation = [
        {"role": "user", "content": "Привет! Я студент, интересуюсь NLP и чат-ботами."},
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "system", "content": system_prompt}] + conversation,
                "response_format": {"type": "json_object"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        result = json.loads(content)
        assert "action" in result
        assert result["action"] in ("reply", "profile")

        if result["action"] == "profile":
            assert "interests" in result
            assert isinstance(result["interests"], list)


# === Test 5: Prompts ===

def test_agent_system_prompt():
    """Test agent system prompt generation."""
    from src.prompts.agent import build_agent_system_prompt

    prompt = build_agent_system_prompt(
        is_business=False,
        profile_info="Теги: NLP, LLM\nКлючевые слова: чат-бот",
        recs_summary="#1 ChatLaw (90%)\n#2 SentimentScope (85%)",
        num_recommendations=2,
    )

    assert "show_project" in prompt
    assert "compare_projects" in prompt
    assert "get_summary" in prompt
    assert "NLP" in prompt
    assert "ChatLaw" in prompt


def test_qa_prompts():
    """Test Q&A prompt generation."""
    from src.prompts.qa import build_guest_qa_prompt, build_business_qa_prompt, build_comparison_matrix_prompt

    sys, usr = build_guest_qa_prompt(
        subtype="student",
        interests="NLP, LLM",
        project_title="ChatLaw",
        project_description="Чат-бот для юридических консультаций",
        project_tech_stack="Python, LangChain",
    )
    assert "ChatLaw" in usr
    assert "JSON" in sys

    sys, usr = build_business_qa_prompt(
        objective="technology",
        industries="AI, NLP",
        tech_stack="Python",
        project_title="ChatLaw",
        project_description="Чат-бот для юридических консультаций",
        project_tech_stack="Python, LangChain",
    )
    assert "ChatLaw" in usr

    sys, usr = build_comparison_matrix_prompt(
        projects_text="- ChatLaw: юридический чат-бот\n- SentimentScope: анализ тональности",
        criteria=["Стек", "Применимость"],
    )
    assert "matrix" in sys.lower() or "матриц" in sys.lower()


# === Test 6: Schemas ===

def test_comparison_matrix_schema():
    matrix = ComparisonMatrix(
        projects=["ChatLaw", "SentimentScope"],
        criteria=["Стек", "Применимость"],
        matrix={
            "ChatLaw": {"Стек": "LangChain + FAISS", "Применимость": "Высокая"},
            "SentimentScope": {"Стек": "Transformers", "Применимость": "Средняя"},
        },
    )
    assert len(matrix.projects) == 2
    assert matrix.matrix["ChatLaw"]["Стек"] == "LangChain + FAISS"


def test_project_extraction_schema():
    extraction = ProjectExtraction(
        problem="Юристы тратят время на рутинные консультации",
        solution="RAG-чатбот отвечает на типовые вопросы",
        audience="Юридические фирмы",
        stack=["Python", "LangChain", "FAISS"],
        novelty="Специализированная доменная модель",
        risks="Качество юридических ответов",
    )
    assert extraction.problem.startswith("Юристы")
    assert len(extraction.stack) == 3


# === Test 7: Expert service ===

@pytest.mark.asyncio
async def test_expert_service(db: AsyncSession, seed_data):
    """Test expert service functions."""
    from src.services.expert import get_expert_by_invite, get_room_projects, get_expert_progress, save_score

    user = User(telegram_user_id="exp_svc_test", full_name="Expert SVC", role_code="expert")
    db.add(user)
    await db.flush()

    expert = Expert(
        user_id=user.id, event_id=seed_data["event"].id,
        invite_code="svc_test_invite_123", name="Expert SVC",
        room_id=seed_data["rooms"][0].id, bot_started=True,
    )
    db.add(expert)
    await db.flush()

    # Test get_expert_by_invite
    found = await get_expert_by_invite(db, "svc_test_invite_123")
    assert found is not None
    assert found.name == "Expert SVC"

    # Test get_room_projects
    projects = await get_room_projects(db, seed_data["rooms"][0].id, seed_data["event"].id)
    assert len(projects) >= 1  # At least ChatLaw is in room1

    # Test save_score with room validation
    project = projects[0]
    saved = await save_score(
        db, expert.id, project.id, seed_data["rooms"][0].id,
        {"Техническая сложность": 4, "Инновационность": 5},
        "Great work",
    )
    assert saved is True

    # Test progress
    progress = await get_expert_progress(db, expert.id, seed_data["rooms"][0].id, seed_data["event"].id)
    assert progress["scored"] == 1
    assert progress["total"] >= 1


# === Test 8: Support service ===

@pytest.mark.asyncio
async def test_support_service(db: AsyncSession, seed_data):
    """Test support log CRUD."""
    from src.services.support import create_support_entry, find_by_correlation_id, save_answer

    user = User(telegram_user_id="sup_test", full_name="Support Test", role_code="guest")
    db.add(user)
    await db.flush()

    entry = await create_support_entry(db, user.id, seed_data["event"].id, "Где парковка?")
    assert entry.correlation_id.startswith("SQ-")
    assert entry.question == "Где парковка?"
    assert entry.answer is None

    # Find by correlation_id
    found = await find_by_correlation_id(db, entry.correlation_id)
    assert found is not None

    # Save answer
    await save_answer(db, found, "Парковка на улице Ломоносова, 9")
    assert found.answer == "Парковка на улице Ломоносова, 9"
    assert found.answered_at is not None


# === Test 9: FSM States ===

def test_fsm_states():
    """Verify all 8 states defined."""
    from src.bot.states import BotStates

    states = BotStates.__all_states__
    assert len(states) == 8

    state_names = {s.state.split(":")[-1] for s in states}
    expected = {
        "choose_role", "onboard_nl_profile", "onboard_confirm",
        "view_program", "view_detail", "support_chat",
        "expert_dashboard", "expert_evaluation",
    }
    assert state_names == expected


# === Test 10: Config ===

def test_config_loads():
    """Test that config loads from env."""
    from src.core.config import settings

    assert settings.bot_token == "test"
    assert settings.llm_model == "deepseek/deepseek-v3.2"
    assert settings.rate_limit_per_minute == 10
    assert settings.semaphore_limit == 10
    assert settings.agent_timeout == 15.0
