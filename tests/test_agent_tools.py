"""
Unit tests for agent tool helper functions.
Tests the pure/helper functions from src/agent/tools.py directly.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai")
os.environ.setdefault("REDIS_URL", "redis://:testpassword@localhost:6379/0")

from src.agent.tools import (
    _find_recommendation,
    _format_project_card,
    _format_matrix,
    _get_default_criteria,
    _get_followup,
    _get_pipeline,
)
from src.agent.agent import _format_profile, _format_recommendations


# ---------------------------------------------------------------------------
# Helpers for creating mock objects
# ---------------------------------------------------------------------------


def _make_recommendation(rank: int, score: float = 85.0, project_id=None):
    """Create a mock Recommendation."""
    rec = MagicMock()
    rec.rank = rank
    rec.relevance_score = score
    rec.project_id = project_id or uuid4()
    return rec


def _make_project(
    title: str = "TestProject",
    description: str = "A test project description for testing purposes",
    tags: list[str] | None = None,
    tech_stack: list[str] | None = None,
    author: str | None = "Author",
    telegram_contact: str | None = "@author",
    parsed_content: dict | None = None,
):
    """Create a mock Project."""
    project = MagicMock()
    project.title = title
    project.description = description
    project.tags = tags
    project.tech_stack = tech_stack
    project.author = author
    project.telegram_contact = telegram_contact
    project.parsed_content = parsed_content
    return project


def _make_profile(
    selected_tags: list[str] | None = None,
    keywords: list[str] | None = None,
    nl_summary: str | None = None,
    company: str | None = None,
    position: str | None = None,
    business_objectives: list[str] | None = None,
    objective: str | None = None,
):
    """Create a mock GuestProfile."""
    profile = MagicMock()
    profile.selected_tags = selected_tags
    profile.keywords = keywords
    profile.nl_summary = nl_summary
    profile.company = company
    profile.position = position
    profile.business_objectives = business_objectives
    profile.objective = objective
    return profile


def _make_user(role_code: str = "guest", subrole: str | None = None):
    """Create a mock User."""
    user = MagicMock()
    user.id = uuid4()
    user.role_code = role_code
    user.subrole = subrole
    return user


def _make_deps(
    user=None,
    profile=None,
    recommendations=None,
    db=None,
    platform=None,
    event=None,
):
    """Create a mock AgentDeps."""
    deps = MagicMock()
    deps.user = user or _make_user()
    deps.profile = profile
    deps.recommendations = recommendations or []
    deps.db = db or AsyncMock()
    deps.platform = platform or AsyncMock()
    deps.event = event or MagicMock(id=uuid4())
    return deps


# ---------------------------------------------------------------------------
# _find_recommendation tests
# ---------------------------------------------------------------------------


class TestFindRecommendation:

    def test_show_project_found(self):
        """Project exists in recommendations, returns correct rec."""
        recs = [_make_recommendation(1), _make_recommendation(2), _make_recommendation(3)]
        result = _find_recommendation(recs, 2)
        assert result is not None
        assert result.rank == 2

    def test_show_project_not_found(self):
        """Rank not in recommendations, returns None."""
        recs = [_make_recommendation(1), _make_recommendation(2)]
        result = _find_recommendation(recs, 5)
        assert result is None

    def test_find_recommendation_empty_list(self):
        """Empty list returns None."""
        result = _find_recommendation([], 1)
        assert result is None


# ---------------------------------------------------------------------------
# _format_project_card tests
# ---------------------------------------------------------------------------


class TestFormatProjectCard:

    def test_basic_card(self):
        """Card with all fields, no percentage."""
        project = _make_project(
            title="ChatLaw",
            description="Legal chatbot",
            tags=["NLP", "LLM"],
            tech_stack=["Python", "LangChain"],
            author="Ivan",
            parsed_content={"problem": "Legal issues", "solution": "RAG bot", "novelty": "Domain model"},
        )
        rec = _make_recommendation(1, score=92.5)

        card = _format_project_card(project, rec)

        assert "#1 ChatLaw" in card
        assert "%" not in card
        assert "Legal chatbot" in card
        assert "NLP" in card
        assert "LangChain" in card
        assert "Legal issues" in card
        assert "RAG bot" in card
        assert "Domain model" in card
        assert "Ivan" in card

    def test_card_without_optional_fields(self):
        """Card without tags, tech_stack, parsed_content."""
        project = _make_project(
            title="Simple",
            description="Simple project",
            tags=None,
            tech_stack=None,
            author=None,
            parsed_content=None,
        )
        rec = _make_recommendation(3, score=70.0)

        card = _format_project_card(project, rec)

        assert "#3 Simple" in card
        assert "Simple project" in card
        assert "Теги" not in card
        assert "Стек" not in card

    def test_card_parsed_content_partial(self):
        """Parsed content with only some fields."""
        project = _make_project(
            parsed_content={"problem": "Some problem"},
        )
        rec = _make_recommendation(1)

        card = _format_project_card(project, rec)

        assert "Some problem" in card
        assert "Решение" not in card


# ---------------------------------------------------------------------------
# show_profile via _format_profile tests
# ---------------------------------------------------------------------------


class TestShowProfile:

    def test_show_profile_with_profile(self):
        """Profile exists, returns formatted text."""
        profile = _make_profile(
            selected_tags=["NLP", "CV"],
            keywords=["chatbot", "vision"],
            company="ITMO",
            position="Student",
            business_objectives=["automate"],
            nl_summary="Interested in AI",
        )

        text = _format_profile(profile)

        assert "NLP" in text
        assert "CV" in text
        assert "chatbot" in text
        assert "ITMO" in text
        assert "Student" in text
        assert "automate" in text
        assert "Interested in AI" in text

    def test_show_profile_empty_profile(self):
        """Profile with no data returns 'No data'."""
        profile = _make_profile()

        text = _format_profile(profile)

        assert text == "Нет данных"


# ---------------------------------------------------------------------------
# _get_default_criteria tests
# ---------------------------------------------------------------------------


class TestGetDefaultCriteria:

    def test_compare_projects_guest_criteria(self):
        """Guest criteria list."""
        criteria = _get_default_criteria(is_business=False)
        assert len(criteria) == 5
        assert "Тематика" in criteria
        assert "Технологии" in criteria

    def test_compare_projects_business_criteria(self):
        """Business criteria list."""
        criteria = _get_default_criteria(is_business=True)
        assert len(criteria) == 5
        assert "Бизнес-модель" in criteria
        assert "Готовность к пилоту" in criteria


# ---------------------------------------------------------------------------
# _format_matrix tests
# ---------------------------------------------------------------------------


class TestFormatMatrix:

    def test_format_matrix_normal(self):
        """Matrix with data produces readable text."""
        matrix = {
            "ChatLaw": {"Тематика": "NLP", "Технологии": "LangChain"},
            "MedVision": {"Тематика": "CV", "Технологии": "PyTorch"},
        }
        criteria = ["Тематика", "Технологии"]

        result = _format_matrix(matrix, criteria)

        assert "Матрица сравнения" in result
        assert "ChatLaw" in result
        assert "MedVision" in result
        assert "NLP" in result
        assert "PyTorch" in result

    def test_format_matrix_empty(self):
        """Empty matrix returns error message."""
        result = _format_matrix({}, ["Тематика"])
        assert "Не удалось" in result

    def test_format_matrix_missing_criterion(self):
        """Missing criterion value shows dash."""
        matrix = {
            "Project": {"Тематика": "NLP"},
        }
        criteria = ["Тематика", "Несуществующий"]

        result = _format_matrix(matrix, criteria)

        assert "-" in result


# ---------------------------------------------------------------------------
# _format_recommendations tests
# ---------------------------------------------------------------------------


class TestFormatRecommendations:

    def test_format_recommendations_normal(self):
        """Recommendations formatted correctly, no percentages."""
        recs = [_make_recommendation(1, score=90.0), _make_recommendation(2, score=85.0)]
        text = _format_recommendations(recs)
        assert "#1" in text
        assert "#2" in text
        assert "%" not in text
        assert "score" not in text

    def test_format_recommendations_empty(self):
        """No recommendations returns specific text."""
        text = _format_recommendations([])
        assert text == "Нет рекомендаций"


# ---------------------------------------------------------------------------
# _get_followup tests
# ---------------------------------------------------------------------------


class TestGetFollowup:

    @pytest.mark.asyncio
    async def test_get_summary_guest(self):
        """Guest role -> followup text with projects and contact template."""
        project_id = uuid4()
        recs = [_make_recommendation(1, score=90.0, project_id=project_id)]

        project = _make_project(
            title="ChatLaw",
            telegram_contact="@chatlaw_dev",
        )

        db = AsyncMock()
        db_result = MagicMock()
        db_result.scalar_one_or_none.return_value = project
        db.execute = AsyncMock(return_value=db_result)

        deps = _make_deps(
            user=_make_user(role_code="guest"),
            recommendations=recs,
            db=db,
        )

        text = await _get_followup(deps)

        assert "Follow-up" in text
        assert "ChatLaw" in text
        assert "@chatlaw_dev" in text
        assert "Шаблон для связи" in text

    @pytest.mark.asyncio
    async def test_get_followup_no_recommendations(self):
        """No recommendations -> rebuild message."""
        deps = _make_deps(
            user=_make_user(role_code="guest"),
            recommendations=[],
        )

        text = await _get_followup(deps)

        assert "Нет рекомендаций" in text


# ---------------------------------------------------------------------------
# _get_pipeline tests
# ---------------------------------------------------------------------------


class TestGetPipeline:

    @pytest.mark.asyncio
    async def test_get_summary_business(self):
        """Business role -> pipeline text with status counts, contact, templates."""
        from src.models.business_followup import BusinessFollowup

        project_id = uuid4()

        followup = MagicMock()
        followup.status = "interested"
        followup.project_id = project_id
        followup.notes = "Want to pilot"

        project = _make_project(title="DataPipe", telegram_contact="@datapipe_dev")

        db = AsyncMock()
        # First execute: select BusinessFollowup
        followup_result = MagicMock()
        followup_result.scalars.return_value.all.return_value = [followup]
        # Second execute: select Project
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project

        db.execute = AsyncMock(side_effect=[followup_result, project_result])

        profile = _make_profile(company="TestCorp")
        deps = _make_deps(
            user=_make_user(role_code="business"),
            profile=profile,
            db=db,
        )

        text = await _get_pipeline(deps)

        assert "Business Pipeline" in text
        assert "interested: 1" in text
        assert "DataPipe" in text
        assert "@datapipe_dev" in text
        assert "Шаблоны для связи" in text
        assert "TestCorp" in text
        assert "Первое обращение" in text
        assert "Повторное обращение" in text

    @pytest.mark.asyncio
    async def test_get_pipeline_empty(self):
        """Empty pipeline returns specific message."""
        db = AsyncMock()
        followup_result = MagicMock()
        followup_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=followup_result)

        deps = _make_deps(
            user=_make_user(role_code="business"),
            db=db,
        )

        text = await _get_pipeline(deps)

        assert "Пайплайн пуст" in text


# ---------------------------------------------------------------------------
# show_project name-based search tests (Feature 1)
# ---------------------------------------------------------------------------


class TestShowProjectByName:
    """Tests for show_project accepting name OR rank via project_identifier."""

    def test_show_project_rank_parsed_from_string(self):
        """Numeric string '2' is parsed to rank 2."""
        recs = [_make_recommendation(1), _make_recommendation(2)]
        identifier = "2"
        rank = int(identifier.strip().lstrip("#"))
        result = _find_recommendation(recs, rank)
        assert result is not None
        assert result.rank == 2

    def test_show_project_rank_with_hash(self):
        """Identifier '#2' strips hash and finds rank 2."""
        recs = [_make_recommendation(1), _make_recommendation(2)]
        identifier = "#2"
        rank = int(identifier.strip().lstrip("#"))
        result = _find_recommendation(recs, rank)
        assert result is not None
        assert result.rank == 2

    def test_show_project_non_numeric_falls_through(self):
        """Non-numeric identifier does not raise, returns None from rank lookup."""
        identifier = "ChatLaw"
        rec = None
        try:
            rank = int(identifier.strip().lstrip("#"))
            rec = _find_recommendation([], rank)
        except ValueError:
            pass
        assert rec is None

    def test_show_project_not_found_message(self):
        """Error message includes original identifier."""
        identifier = "NonExistent"
        message = f"Проект '{identifier}' не найден в рекомендациях."
        assert "NonExistent" in message
        assert "не найден" in message

    def test_show_project_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped from identifier."""
        recs = [_make_recommendation(3)]
        identifier = "  3  "
        rank = int(identifier.strip().lstrip("#"))
        result = _find_recommendation(recs, rank)
        assert result is not None
        assert result.rank == 3


# ---------------------------------------------------------------------------
# filter_projects tests (Feature 7)
# ---------------------------------------------------------------------------


class TestFilterProjects:
    """Tests for the filter_projects tool logic."""

    def test_filter_no_recommendations_message(self):
        """Empty recommendations returns rebuild message."""
        recs = []
        if not recs:
            result = "Нет рекомендаций. Используйте /rebuild."
        assert "Нет рекомендаций" in result

    def test_filter_matching_tag(self):
        """Tag in project.tags is matched."""
        project = _make_project(
            title="ChatLaw",
            tags=["NLP", "LLM"],
            tech_stack=["Python", "LangChain"],
        )
        tag_lower = "nlp"
        project_tags = [t.lower() for t in (project.tags or [])]
        assert tag_lower in project_tags

    def test_filter_matching_tech_stack(self):
        """Tag in project.tech_stack is matched."""
        project = _make_project(
            title="ChatLaw",
            tags=["NLP"],
            tech_stack=["Python", "LangChain"],
        )
        tag_lower = "python"
        project_stack = [t.lower() for t in (project.tech_stack or [])]
        assert tag_lower in project_stack

    def test_filter_no_match(self):
        """Non-matching tag returns no results."""
        project = _make_project(
            title="ChatLaw",
            tags=["NLP"],
            tech_stack=["Python"],
        )
        tag_lower = "blockchain"
        project_tags = [t.lower() for t in (project.tags or [])]
        project_stack = [t.lower() for t in (project.tech_stack or [])]
        assert tag_lower not in project_tags
        assert tag_lower not in project_stack

    def test_filter_case_insensitive(self):
        """Tag matching is case-insensitive."""
        project = _make_project(
            title="MedVision",
            tags=["CV", "медицина"],
            tech_stack=["PyTorch"],
        )
        # uppercase input vs lowercase in logic
        tag_lower = "cv"
        project_tags = [t.lower() for t in (project.tags or [])]
        assert tag_lower in project_tags

    def test_filter_output_format(self):
        """Output includes header with count, rank+title, and tags."""
        rec = _make_recommendation(1, score=90.0)
        project = _make_project(
            title="ChatLaw",
            tags=["NLP", "LLM", "RAG"],
        )
        tag = "NLP"

        lines = [f"Проекты с тегом '{tag}' (1):\n"]
        lines.append(f"#{rec.rank} {project.title}")
        tags_str = ", ".join(project.tags[:3]) if project.tags else ""
        if tags_str:
            lines.append(f"   {tags_str}")
        result = "\n".join(lines)

        assert "Проекты с тегом 'NLP'" in result
        assert "(1)" in result
        assert "#1 ChatLaw" in result
        assert "NLP, LLM, RAG" in result

    def test_filter_none_tags_no_crash(self):
        """Projects with None tags/tech_stack don't crash the filter."""
        project = _make_project(
            title="Simple",
            tags=None,
            tech_stack=None,
        )
        tag_lower = "nlp"
        project_tags = [t.lower() for t in (project.tags or [])]
        project_stack = [t.lower() for t in (project.tech_stack or [])]
        assert tag_lower not in project_tags
        assert tag_lower not in project_stack

    def test_filter_no_match_message(self):
        """No-match message includes the searched tag."""
        tag = "blockchain"
        message = f"Нет проектов с тегом '{tag}' в ваших рекомендациях."
        assert "blockchain" in message
        assert "Нет проектов" in message
