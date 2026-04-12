"""
Unit tests for src/services/github_analyzer.py.
Tests parse_github_url, gh_api (mocked), analyze_repo, and drill-down functions.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai")
os.environ.setdefault("REDIS_URL", "redis://:testpassword@localhost:6379/0")

from src.services.github_analyzer import (
    MAX_FILE_CHARS,
    analyze_repo,
    fetch_commits,
    fetch_contributors,
    fetch_file,
    fetch_tree,
    gh_api,
    parse_github_url,
)


# ---------------------------------------------------------------------------
# parse_github_url tests
# ---------------------------------------------------------------------------


class TestParseGithubUrl:

    def test_https_url(self):
        result = parse_github_url("https://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_http_url(self):
        result = parse_github_url("http://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_url_with_git_suffix(self):
        result = parse_github_url("https://github.com/owner/repo.git")
        assert result == ("owner", "repo")

    def test_url_with_trailing_slash(self):
        result = parse_github_url("https://github.com/owner/repo/")
        assert result == ("owner", "repo")

    def test_url_with_whitespace(self):
        result = parse_github_url("  https://github.com/owner/repo  ")
        assert result == ("owner", "repo")

    def test_url_with_subpath(self):
        result = parse_github_url("https://github.com/owner/repo/tree/main")
        assert result == ("owner", "repo")

    def test_invalid_url(self):
        result = parse_github_url("https://gitlab.com/owner/repo")
        assert result is None

    def test_empty_string(self):
        result = parse_github_url("")
        assert result is None

    def test_non_url(self):
        result = parse_github_url("not a url at all")
        assert result is None


# ---------------------------------------------------------------------------
# gh_api tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestGhApi:

    @pytest.mark.asyncio
    async def test_successful_call(self):
        """gh api returns valid JSON."""
        expected = {"name": "repo", "stars": 42}

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(json.dumps(expected).encode(), b"")
        )

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("src.services.github_analyzer.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            with patch("src.services.github_analyzer.asyncio.wait_for",
                        side_effect=fake_wait_for):
                result = await gh_api("repos/owner/repo")

        assert result == expected

    @pytest.mark.asyncio
    async def test_nonzero_return_code(self):
        """gh api fails with non-zero exit code."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Not Found")
        )

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("src.services.github_analyzer.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            with patch("src.services.github_analyzer.asyncio.wait_for",
                        side_effect=fake_wait_for):
                result = await gh_api("repos/owner/nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout(self):
        """gh api times out."""
        with patch("src.services.github_analyzer.asyncio.wait_for",
                    side_effect=asyncio.TimeoutError):
            result = await gh_api("repos/owner/repo", timeout=0.1)

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        """gh api returns non-JSON output."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"not json", b"")
        )

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("src.services.github_analyzer.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=mock_proc):
            with patch("src.services.github_analyzer.asyncio.wait_for",
                        side_effect=fake_wait_for):
                result = await gh_api("repos/owner/repo")

        assert result is None

    @pytest.mark.asyncio
    async def test_gh_not_found(self):
        """gh CLI binary not found."""
        with patch("src.services.github_analyzer.asyncio.wait_for",
                    side_effect=FileNotFoundError):
            result = await gh_api("repos/owner/repo")

        assert result is None

    @pytest.mark.asyncio
    async def test_token_passed_as_env(self):
        """Token is passed via GH_TOKEN env var."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b'{"ok": true}', b"")
        )

        captured_env = {}

        original_create = asyncio.create_subprocess_exec

        async def fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}) or {})
            return mock_proc

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("src.services.github_analyzer.asyncio.create_subprocess_exec",
                    side_effect=fake_exec):
            with patch("src.services.github_analyzer.asyncio.wait_for",
                        side_effect=fake_wait_for):
                await gh_api("repos/owner/repo", token="ghp_test123")

        assert captured_env.get("GH_TOKEN") == "ghp_test123"


# ---------------------------------------------------------------------------
# analyze_repo tests (mocked gh_api)
# ---------------------------------------------------------------------------


class TestAnalyzeRepo:

    @pytest.mark.asyncio
    async def test_repo_not_found(self):
        """analyze_repo returns error dict when repo not found."""
        with patch("src.services.github_analyzer.gh_api", return_value=None):
            result = await analyze_repo("owner", "nonexistent")

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_analysis(self):
        """Full analysis returns expected keys."""
        meta = {
            "default_branch": "main",
            "stargazers_count": 42,
            "forks_count": 5,
            "open_issues_count": 3,
            "fork": False,
            "license": {"spdx_id": "MIT"},
            "created_at": "2025-01-01T00:00:00Z",
            "pushed_at": "2026-04-01T00:00:00Z",
            "language": "Python",
        }
        contributors = [
            {"login": "dev1", "contributions": 80},
            {"login": "dev2", "contributions": 20},
        ]
        languages = {"Python": 10000, "JavaScript": 2000}
        commits = [
            {
                "sha": "abc1234567890",
                "commit": {
                    "message": "Initial commit",
                    "author": {"name": "dev1", "date": "2026-04-01T10:00:00Z"},
                },
            },
        ]
        topics = {"names": ["ai", "nlp"]}
        tree = {
            "tree": [
                {"path": "README.md", "type": "blob"},
                {"path": "src", "type": "tree"},
                {"path": "src/main.py", "type": "blob"},
                {"path": "tests", "type": "tree"},
                {"path": "tests/test_main.py", "type": "blob"},
                {"path": ".github/workflows/ci.yml", "type": "blob"},
                {"path": "Dockerfile", "type": "blob"},
                {"path": "requirements.txt", "type": "blob"},
            ],
        }

        call_count = 0

        async def mock_gh_api(endpoint, token="", timeout=15.0):
            nonlocal call_count
            call_count += 1
            if "contributors" in endpoint:
                return contributors
            if "languages" in endpoint:
                return languages
            if "commits" in endpoint:
                return commits
            if "topics" in endpoint:
                return topics
            if "trees" in endpoint:
                return tree
            return meta

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await analyze_repo("owner", "repo")

        assert result["full_name"] == "owner/repo"
        assert result["stars"] == 42
        assert result["forks_count"] == 5
        assert result["primary_language"] == "Python"
        assert result["has_tests"] is True
        assert result["has_ci"] is True
        assert result["has_docker"] is True
        assert result["has_readme"] is True
        assert result["has_requirements"] is True
        assert result["total_commits"] == 100  # 80 + 20
        assert result["contributors_count"] == 2
        assert result["bus_factor"] == 2  # dev1 (80%) and dev2 (20%) both > 10%
        assert len(result["contributors"]) == 2
        assert result["license"] == "MIT"
        assert "ai" in result["topics"]
        assert result["health_score"] > 0
        assert "analyzed_at" in result

    @pytest.mark.asyncio
    async def test_red_flags_no_tests_no_readme(self):
        """Red flags generated for missing tests and README."""
        meta = {
            "default_branch": "main",
            "stargazers_count": 0,
            "forks_count": 0,
            "open_issues_count": 0,
            "fork": False,
            "license": None,
            "created_at": "2026-04-10T00:00:00Z",
            "pushed_at": "2026-04-10T00:00:00Z",
            "language": "Python",
        }

        async def mock_gh_api(endpoint, token="", timeout=15.0):
            if "contributors" in endpoint:
                return [{"login": "solo", "contributions": 5}]
            if "languages" in endpoint:
                return {"Python": 1000}
            if "commits" in endpoint:
                return []
            if "topics" in endpoint:
                return {"names": []}
            if "trees" in endpoint:
                return {"tree": [{"path": "main.py", "type": "blob"}]}
            return meta

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await analyze_repo("owner", "repo")

        categories = [f["category"] for f in result["red_flags"]]
        assert "quality" in categories  # no tests + no README
        assert "team" in categories     # single contributor
        assert "legal" in categories    # no license
        assert "timeline" in categories  # created < 14 days ago
        assert "scope" in categories    # < 10 commits

    @pytest.mark.asyncio
    async def test_fork_red_flag(self):
        """Fork detection adds red flag."""
        meta = {
            "default_branch": "main",
            "stargazers_count": 0,
            "forks_count": 0,
            "open_issues_count": 0,
            "fork": True,
            "license": {"spdx_id": "MIT"},
            "created_at": "2025-01-01T00:00:00Z",
            "pushed_at": "2026-04-01T00:00:00Z",
            "language": "Python",
        }

        async def mock_gh_api(endpoint, token="", timeout=15.0):
            if endpoint.startswith("repos/") and "contributors" not in endpoint and "languages" not in endpoint and "commits" not in endpoint and "topics" not in endpoint and "trees" not in endpoint:
                return meta
            if "contributors" in endpoint:
                return [{"login": "dev1", "contributions": 100}]
            if "languages" in endpoint:
                return {}
            if "commits" in endpoint:
                return []
            if "topics" in endpoint:
                return {"names": []}
            if "trees" in endpoint:
                return {"tree": [{"path": "README.md", "type": "blob"}]}
            return meta

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await analyze_repo("owner", "repo")

        categories = [f["category"] for f in result["red_flags"]]
        assert "originality" in categories


# ---------------------------------------------------------------------------
# fetch_file tests
# ---------------------------------------------------------------------------


class TestFetchFile:

    @pytest.mark.asyncio
    async def test_file_found(self):
        """Successful file fetch returns decoded content."""
        import base64

        content = "print('hello world')\n"
        encoded = base64.b64encode(content.encode()).decode()
        api_response = {
            "type": "file",
            "size": len(content),
            "content": encoded,
        }

        with patch("src.services.github_analyzer.gh_api", return_value=api_response):
            result = await fetch_file("owner", "repo", "main.py")

        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """Missing file returns error message."""
        with patch("src.services.github_analyzer.gh_api", return_value=None):
            result = await fetch_file("owner", "repo", "nonexistent.py")

        assert "not found" in result.lower() or "Not found" in result

    @pytest.mark.asyncio
    async def test_file_is_directory(self):
        """Directory path returns helpful message."""
        api_response = {"type": "dir", "size": 0}

        with patch("src.services.github_analyzer.gh_api", return_value=api_response):
            result = await fetch_file("owner", "repo", "src")

        assert "directory" in result.lower()

    @pytest.mark.asyncio
    async def test_file_too_large(self):
        """Large file returns size warning."""
        api_response = {"type": "file", "size": 600_000, "content": ""}

        with patch("src.services.github_analyzer.gh_api", return_value=api_response):
            result = await fetch_file("owner", "repo", "big.bin")

        assert "too large" in result.lower() or "large" in result.lower()

    @pytest.mark.asyncio
    async def test_file_content_truncated(self):
        """Content > MAX_FILE_CHARS is truncated."""
        import base64

        long_content = "x" * (MAX_FILE_CHARS + 500)
        encoded = base64.b64encode(long_content.encode()).decode()
        api_response = {
            "type": "file",
            "size": len(long_content),
            "content": encoded,
        }

        with patch("src.services.github_analyzer.gh_api", return_value=api_response):
            result = await fetch_file("owner", "repo", "big.py")

        assert "truncated" in result.lower()
        assert len(result) < len(long_content)


# ---------------------------------------------------------------------------
# fetch_tree tests
# ---------------------------------------------------------------------------


class TestFetchTree:

    @pytest.mark.asyncio
    async def test_tree_success(self):
        """Tree fetch returns formatted output."""
        meta = {"default_branch": "main"}
        tree_data = {
            "tree": [
                {"path": "README.md", "type": "blob"},
                {"path": "src", "type": "tree"},
                {"path": "src/main.py", "type": "blob"},
            ],
        }

        call_count = 0

        async def mock_gh_api(endpoint, token="", timeout=15.0):
            nonlocal call_count
            call_count += 1
            if "trees" in endpoint:
                return tree_data
            return meta

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await fetch_tree("owner", "repo")

        assert "README.md" in result
        assert "src/" in result
        assert "src/main.py" in result

    @pytest.mark.asyncio
    async def test_tree_with_path_filter(self):
        """Tree with path prefix filters entries."""
        meta = {"default_branch": "main"}
        tree_data = {
            "tree": [
                {"path": "src", "type": "tree"},
                {"path": "src/main.py", "type": "blob"},
                {"path": "src/utils.py", "type": "blob"},
                {"path": "tests/test_main.py", "type": "blob"},
            ],
        }

        async def mock_gh_api(endpoint, token="", timeout=15.0):
            if "trees" in endpoint:
                return tree_data
            return meta

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await fetch_tree("owner", "repo", path="src")

        assert "src/main.py" in result
        assert "test_main.py" not in result

    @pytest.mark.asyncio
    async def test_tree_not_found(self):
        """Failed tree fetch returns error."""
        async def mock_gh_api(endpoint, token="", timeout=15.0):
            return None

        with patch("src.services.github_analyzer.gh_api", side_effect=mock_gh_api):
            result = await fetch_tree("owner", "repo")

        assert "Cannot fetch" in result


# ---------------------------------------------------------------------------
# fetch_commits tests
# ---------------------------------------------------------------------------


class TestFetchCommits:

    @pytest.mark.asyncio
    async def test_commits_success(self):
        """Commits fetch returns formatted output."""
        commits_data = [
            {
                "sha": "abc1234567890",
                "commit": {
                    "message": "Add feature X",
                    "author": {"name": "dev1", "date": "2026-04-01T10:00:00Z"},
                },
            },
            {
                "sha": "def5678901234",
                "commit": {
                    "message": "Fix bug Y",
                    "author": {"name": "dev2", "date": "2026-03-31T15:00:00Z"},
                },
            },
        ]

        with patch("src.services.github_analyzer.gh_api", return_value=commits_data):
            result = await fetch_commits("owner", "repo")

        assert "abc1234" in result
        assert "Add feature X" in result
        assert "dev1" in result
        assert "Fix bug Y" in result

    @pytest.mark.asyncio
    async def test_commits_not_found(self):
        """Failed commits fetch returns error."""
        with patch("src.services.github_analyzer.gh_api", return_value=None):
            result = await fetch_commits("owner", "repo")

        assert "Cannot fetch" in result


# ---------------------------------------------------------------------------
# fetch_contributors tests
# ---------------------------------------------------------------------------


class TestFetchContributors:

    @pytest.mark.asyncio
    async def test_contributors_success(self):
        """Contributors fetch returns formatted output."""
        contribs_data = [
            {"login": "dev1", "contributions": 80},
            {"login": "dev2", "contributions": 20},
        ]

        with patch("src.services.github_analyzer.gh_api", return_value=contribs_data):
            result = await fetch_contributors("owner", "repo")

        assert "dev1" in result
        assert "80 commits" in result
        assert "dev2" in result
        assert "20 commits" in result

    @pytest.mark.asyncio
    async def test_contributors_not_found(self):
        """Failed contributors fetch returns error."""
        with patch("src.services.github_analyzer.gh_api", return_value=None):
            result = await fetch_contributors("owner", "repo")

        assert "Cannot fetch" in result
