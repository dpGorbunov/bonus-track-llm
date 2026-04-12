"""
Unit tests for artifact parser service.
Tests parse functions, extract_structured, and _build_project_context helper.
"""

import io
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai")
os.environ.setdefault("REDIS_URL", "redis://:testpassword@localhost:6379/0")

from src.schemas.tools import ProjectExtraction, RedFlag
from src.services.artifact_parser import (
    extract_structured,
    parse_github_readme,
    parse_presentation,
)
from src.agent.tools import _build_project_context, _format_project_card


# ---------------------------------------------------------------------------
# RedFlag and ProjectExtraction schema tests
# ---------------------------------------------------------------------------


class TestRedFlagSchema:

    def test_red_flag_creation(self):
        flag = RedFlag(category="metric", description="Unrealistic accuracy", severity="high")
        assert flag.category == "metric"
        assert flag.severity == "high"

    def test_red_flag_to_dict(self):
        flag = RedFlag(category="team", description="Solo dev", severity="low")
        data = flag.model_dump()
        assert data["category"] == "team"
        assert data["description"] == "Solo dev"


class TestProjectExtractionSchema:

    def test_full_extraction(self):
        extraction = ProjectExtraction(
            problem="Legal questions",
            solution="RAG chatbot",
            audience="Lawyers",
            stack=["Python", "LangChain"],
            novelty="Domain-specific RAG",
            risks="Data quality",
            key_metrics=["F1=0.91", "accuracy=94%"],
            production_readiness="mvp",
            team_size=3,
            red_flags=[
                RedFlag(category="metric", description="No validation set", severity="medium")
            ],
        )
        assert extraction.problem == "Legal questions"
        assert len(extraction.key_metrics) == 2
        assert extraction.red_flags[0].category == "metric"

    def test_minimal_extraction(self):
        extraction = ProjectExtraction(
            problem="Problem",
            solution="Solution",
            audience="Users",
            stack=["Python"],
            novelty="Novel approach",
        )
        assert extraction.risks is None
        assert extraction.key_metrics is None
        assert extraction.production_readiness is None
        assert extraction.team_size is None
        assert extraction.red_flags is None

    def test_extraction_model_dump(self):
        extraction = ProjectExtraction(
            problem="P",
            solution="S",
            audience="A",
            stack=["Go"],
            novelty="N",
            key_metrics=["latency=50ms"],
        )
        data = extraction.model_dump()
        assert data["key_metrics"] == ["latency=50ms"]
        assert data["risks"] is None


# ---------------------------------------------------------------------------
# parse_github_readme tests
# ---------------------------------------------------------------------------


class TestParseGithubReadme:

    @pytest.mark.asyncio
    async def test_valid_github_url(self):
        """Mock httpx to return README content."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# My Project\nDescription here"

        with patch("src.services.artifact_parser.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await parse_github_readme("https://github.com/user/repo")

        assert "My Project" in result
        assert "Description here" in result

    @pytest.mark.asyncio
    async def test_invalid_github_url(self):
        """Non-github URL returns empty string."""
        result = await parse_github_readme("https://example.com/not-github")
        assert result == ""

    @pytest.mark.asyncio
    async def test_github_url_with_git_suffix(self):
        """URL with .git suffix is handled."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# Repo"

        with patch("src.services.artifact_parser.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await parse_github_readme("https://github.com/user/repo.git")

        assert "Repo" in result

    @pytest.mark.asyncio
    async def test_github_readme_truncated(self):
        """README is truncated to 3000 chars."""
        long_readme = "x" * 5000
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = long_readme

        with patch("src.services.artifact_parser.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await parse_github_readme("https://github.com/user/repo")

        assert len(result) == 3000

    @pytest.mark.asyncio
    async def test_github_fallback_to_master(self):
        """If main branch 404s, falls back to master."""
        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.text = "# Master README"

        with patch("src.services.artifact_parser.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[resp_404, resp_200])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await parse_github_readme("https://github.com/user/repo")

        assert "Master README" in result


# ---------------------------------------------------------------------------
# parse_presentation tests
# ---------------------------------------------------------------------------


class TestParsePresentation:

    @pytest.mark.asyncio
    async def test_pptx_extension_dispatches(self):
        """URL ending in .pptx calls parse_pptx."""
        with patch("src.services.artifact_parser.parse_pptx", new_callable=AsyncMock) as mock_pptx:
            mock_pptx.return_value = "slide text"
            result = await parse_presentation("https://example.com/file.pptx")
        mock_pptx.assert_awaited_once_with("https://example.com/file.pptx")
        assert result == "slide text"

    @pytest.mark.asyncio
    async def test_pdf_extension_dispatches(self):
        """URL ending in .pdf calls parse_pdf."""
        with patch("src.services.artifact_parser.parse_pdf", new_callable=AsyncMock) as mock_pdf:
            mock_pdf.return_value = "page text"
            result = await parse_presentation("https://example.com/file.pdf")
        mock_pdf.assert_awaited_once_with("https://example.com/file.pdf")
        assert result == "page text"

    @pytest.mark.asyncio
    async def test_unknown_extension_tries_pdf_first(self):
        """Unknown extension tries PDF first."""
        with patch("src.services.artifact_parser.parse_pdf", new_callable=AsyncMock) as mock_pdf:
            mock_pdf.return_value = "pdf content"
            result = await parse_presentation("https://example.com/file.unknown")
        mock_pdf.assert_awaited_once()
        assert result == "pdf content"

    @pytest.mark.asyncio
    async def test_unknown_extension_fallback_to_pptx(self):
        """Unknown extension falls back to PPTX if PDF fails."""
        with patch("src.services.artifact_parser.parse_pdf", new_callable=AsyncMock) as mock_pdf, \
             patch("src.services.artifact_parser.parse_pptx", new_callable=AsyncMock) as mock_pptx:
            mock_pdf.side_effect = Exception("not a pdf")
            mock_pptx.return_value = "pptx content"
            result = await parse_presentation("https://example.com/file.unknown")
        assert result == "pptx content"

    @pytest.mark.asyncio
    async def test_unknown_extension_both_fail(self):
        """If both parsers fail, returns empty string."""
        with patch("src.services.artifact_parser.parse_pdf", new_callable=AsyncMock) as mock_pdf, \
             patch("src.services.artifact_parser.parse_pptx", new_callable=AsyncMock) as mock_pptx:
            mock_pdf.side_effect = Exception("not a pdf")
            mock_pptx.side_effect = Exception("not a pptx")
            result = await parse_presentation("https://example.com/file.unknown")
        assert result == ""


# ---------------------------------------------------------------------------
# extract_structured tests
# ---------------------------------------------------------------------------


class TestExtractStructured:

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        """LLM returns valid JSON, parsed into ProjectExtraction dict."""
        extraction_data = {
            "problem": "Legal queries",
            "solution": "RAG chatbot",
            "audience": "Lawyers",
            "stack": ["Python", "LangChain"],
            "novelty": "Domain RAG",
            "risks": None,
            "key_metrics": ["F1=0.91"],
            "production_readiness": "mvp",
            "team_size": 2,
            "red_flags": None,
        }

        platform = AsyncMock()
        platform.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps(extraction_data)}}],
        })

        result = await extract_structured(
            raw_text="Some raw text from presentation",
            project_title="ChatLaw",
            project_description="Legal chatbot",
            platform_client=platform,
        )

        assert result["problem"] == "Legal queries"
        assert result["solution"] == "RAG chatbot"
        assert result["key_metrics"] == ["F1=0.91"]
        assert result["production_readiness"] == "mvp"

    @pytest.mark.asyncio
    async def test_extraction_with_red_flags(self):
        """Extraction includes red flags."""
        extraction_data = {
            "problem": "Medical images",
            "solution": "CNN classifier",
            "audience": "Doctors",
            "stack": ["PyTorch"],
            "novelty": "Novel architecture",
            "red_flags": [
                {"category": "metric", "description": "Unrealistic accuracy claim", "severity": "high"}
            ],
        }

        platform = AsyncMock()
        platform.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps(extraction_data)}}],
        })

        result = await extract_structured(
            "raw text", "MedVision", "AI for medical images", platform
        )

        assert len(result["red_flags"]) == 1
        assert result["red_flags"][0]["category"] == "metric"
        assert result["red_flags"][0]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_extraction_llm_failure(self):
        """LLM call fails, returns empty dict."""
        platform = AsyncMock()
        platform.chat_completion = AsyncMock(
            side_effect=RuntimeError("LLM unavailable")
        )

        result = await extract_structured(
            "raw text", "Project", "Description", platform
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_extraction_invalid_json(self):
        """LLM returns invalid JSON, returns empty dict."""
        platform = AsyncMock()
        platform.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": "not valid json at all"}}],
        })

        result = await extract_structured(
            "raw text", "Project", "Description", platform
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_extraction_truncates_raw_text(self):
        """Raw text is truncated to 5000 chars in prompt."""
        long_text = "a" * 10000
        platform = AsyncMock()
        platform.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps({
                "problem": "P", "solution": "S", "audience": "A",
                "stack": [], "novelty": "N",
            })}}],
        })

        await extract_structured(long_text, "Title", "Desc", platform)

        call_args = platform.chat_completion.call_args
        user_msg = call_args[1]["messages"][1]["content"]
        # The prompt should not contain the full 10000 chars
        assert len(user_msg) < 10000


# ---------------------------------------------------------------------------
# _build_project_context tests
# ---------------------------------------------------------------------------


class TestBuildProjectContext:

    def _make_project(self, **kwargs):
        project = MagicMock()
        project.title = kwargs.get("title", "TestProject")
        project.description = kwargs.get("description", "A test project")
        project.tags = kwargs.get("tags", None)
        project.tech_stack = kwargs.get("tech_stack", None)
        project.parsed_content = kwargs.get("parsed_content", None)
        project.author = kwargs.get("author", None)
        return project

    def test_basic_context(self):
        project = self._make_project(
            title="ChatLaw",
            description="Legal chatbot with RAG",
            tech_stack=["Python", "LangChain"],
        )
        result = _build_project_context(project)
        assert "ChatLaw" in result
        assert "Legal chatbot" in result
        assert "Python" in result

    def test_context_with_parsed_content(self):
        project = self._make_project(
            title="MedVision",
            description="Medical AI",
            tech_stack=["PyTorch"],
            parsed_content={
                "problem": "Diagnose X-rays",
                "solution": "CNN classifier",
                "key_metrics": ["accuracy=94%", "F1=0.89"],
                "novelty": "Transfer learning",
                "risks": "Small dataset",
                "production_readiness": "prototype",
            },
        )
        result = _build_project_context(project)
        assert "Diagnose X-rays" in result
        assert "CNN classifier" in result
        assert "accuracy=94%" in result
        assert "Transfer learning" in result
        assert "Small dataset" in result
        assert "prototype" in result

    def test_context_without_parsed_content(self):
        project = self._make_project(
            title="Simple",
            description="Simple project",
            parsed_content=None,
        )
        result = _build_project_context(project)
        assert "Simple" in result
        assert "Проблема" not in result

    def test_context_truncates_description(self):
        long_desc = "x" * 500
        project = self._make_project(description=long_desc)
        result = _build_project_context(project, max_desc=100)
        # Description should be truncated
        assert len(result.split(": ", 1)[1].split("\n")[0]) <= 100

    def test_context_partial_parsed_content(self):
        project = self._make_project(
            parsed_content={"problem": "Only problem", "solution": None},
        )
        result = _build_project_context(project)
        assert "Only problem" in result
        assert "Решение" not in result


# ---------------------------------------------------------------------------
# Updated _format_project_card tests (with new fields)
# ---------------------------------------------------------------------------


class TestFormatProjectCardExtended:

    def _make_project(self, **kwargs):
        project = MagicMock()
        project.title = kwargs.get("title", "TestProject")
        project.description = kwargs.get("description", "A test project")
        project.tags = kwargs.get("tags", None)
        project.tech_stack = kwargs.get("tech_stack", None)
        project.parsed_content = kwargs.get("parsed_content", None)
        project.author = kwargs.get("author", None)
        project.telegram_contact = kwargs.get("telegram_contact", None)
        return project

    def _make_rec(self, rank=1):
        rec = MagicMock()
        rec.rank = rank
        return rec

    def test_card_with_audience(self):
        project = self._make_project(
            parsed_content={"audience": "Students"},
        )
        card = _format_project_card(project, self._make_rec())
        assert "Students" in card

    def test_card_with_key_metrics(self):
        project = self._make_project(
            parsed_content={"key_metrics": ["F1=0.91", "latency=50ms"]},
        )
        card = _format_project_card(project, self._make_rec())
        assert "F1=0.91" in card
        assert "latency=50ms" in card

    def test_card_with_production_readiness(self):
        project = self._make_project(
            parsed_content={"production_readiness": "mvp"},
        )
        card = _format_project_card(project, self._make_rec())
        assert "mvp" in card

    def test_card_with_red_flags(self):
        project = self._make_project(
            parsed_content={
                "red_flags": [
                    {"category": "metric", "description": "No validation", "severity": "high"},
                    {"category": "scope", "description": "Too broad", "severity": "medium"},
                ],
            },
        )
        card = _format_project_card(project, self._make_rec())
        assert "No validation" in card
        assert "high" in card
        assert "Too broad" in card

    def test_card_with_all_fields(self):
        project = self._make_project(
            title="FullProject",
            description="Full description",
            tags=["NLP"],
            tech_stack=["Python"],
            author="Author",
            parsed_content={
                "problem": "Big problem",
                "solution": "Smart solution",
                "audience": "Everyone",
                "novelty": "Very novel",
                "key_metrics": ["accuracy=99%"],
                "production_readiness": "production",
                "risks": "None identified",
                "red_flags": [
                    {"category": "metric", "description": "Suspiciously high", "severity": "high"},
                ],
            },
        )
        card = _format_project_card(project, self._make_rec())
        assert "Big problem" in card
        assert "Smart solution" in card
        assert "Everyone" in card
        assert "Very novel" in card
        assert "accuracy=99%" in card
        assert "production" in card
        assert "None identified" in card
        assert "Suspiciously high" in card
        assert "Author" in card
