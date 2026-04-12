"""GitHub repository analysis via gh CLI.

Provides both batch analysis (offline) and real-time drill-down.
All GitHub access through `gh api` subprocess.
"""

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_FILE_CHARS = 3000
MAX_TREE_ENTRIES = 150


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from GitHub URL."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url.strip().rstrip("/"))
    if not match:
        return None
    return match.group(1), match.group(2).rstrip(".git")


async def gh_api(endpoint: str, token: str = "", timeout: float = 15.0) -> dict | list | None:
    """Run `gh api <endpoint>` and return parsed JSON."""
    env: dict[str, str] | None = None
    if token:
        env = {**os.environ, "GH_TOKEN": token}

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "gh", "api", endpoint,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            ),
            timeout=timeout,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:200]
            logger.warning("gh api %s failed: %s", endpoint, err)
            return None

        return json.loads(stdout.decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        logger.warning("gh api %s timeout", endpoint)
        return None
    except json.JSONDecodeError:
        return None
    except FileNotFoundError:
        logger.error("gh CLI not found. Install: https://cli.github.com/")
        return None


async def analyze_repo(owner: str, repo: str, token: str = "") -> dict:
    """Full batch analysis of a GitHub repo. Returns dict for parsed_content["github"]."""

    # Phase 1: metadata
    meta = await gh_api(f"repos/{owner}/{repo}", token)
    if not meta:
        return {"error": f"Repo {owner}/{repo} not found or inaccessible"}

    # Phase 2: parallel fetches
    contributors_task = gh_api(f"repos/{owner}/{repo}/contributors?per_page=20", token)
    languages_task = gh_api(f"repos/{owner}/{repo}/languages", token)
    commits_task = gh_api(f"repos/{owner}/{repo}/commits?per_page=10", token)
    topics_task = gh_api(f"repos/{owner}/{repo}/topics", token)

    contributors, languages, commits, topics = await asyncio.gather(
        contributors_task, languages_task, commits_task, topics_task,
        return_exceptions=True,
    )

    # Safely handle exceptions from gather
    if isinstance(contributors, Exception):
        contributors = None
    if isinstance(languages, Exception):
        languages = None
    if isinstance(commits, Exception):
        commits = None
    if isinstance(topics, Exception):
        topics = None

    # Phase 3: conditional fetches
    default_branch = meta.get("default_branch", "main")

    # File tree
    tree = await gh_api(f"repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1", token)

    # Check for key files
    tree_paths: set[str] = set()
    if tree and isinstance(tree, dict):
        tree_paths = {item["path"] for item in tree.get("tree", []) if isinstance(item, dict)}

    has_tests = any(
        p.startswith("tests/") or p.startswith("test/") or "test_" in p.split("/")[-1]
        for p in tree_paths
    )
    has_ci = any(p.startswith(".github/workflows/") for p in tree_paths)
    has_docker = "Dockerfile" in tree_paths or "docker-compose.yml" in tree_paths
    has_readme = "README.md" in tree_paths or "readme.md" in tree_paths
    has_requirements = (
        "requirements.txt" in tree_paths
        or "pyproject.toml" in tree_paths
        or "package.json" in tree_paths
    )

    # Build contributors list
    contrib_list: list[dict] = []
    total_commits_count = 0
    if isinstance(contributors, list):
        total_commits_count = sum(c.get("contributions", 0) for c in contributors)
        for c in contributors[:10]:
            contribs = c.get("contributions", 0)
            pct = (contribs / total_commits_count * 100) if total_commits_count > 0 else 0
            contrib_list.append({
                "login": c.get("login", "unknown"),
                "contributions": contribs,
                "percentage": round(pct, 1),
            })

    # Recent commits
    recent_commits: list[dict] = []
    if isinstance(commits, list):
        for c in commits[:5]:
            commit_data = c.get("commit", {})
            author_data = commit_data.get("author", {})
            recent_commits.append({
                "sha": c.get("sha", "")[:7],
                "message": commit_data.get("message", "").split("\n")[0][:120],
                "date": author_data.get("date", "")[:10],
                "author": author_data.get("name", "unknown"),
            })

    # File tree (top level + key files)
    file_tree: list[str] = []
    if tree and isinstance(tree, dict):
        for item in tree.get("tree", [])[:MAX_TREE_ENTRIES]:
            if isinstance(item, dict):
                file_tree.append(item["path"])

    # Compute metrics
    created_at = meta.get("created_at", "")
    pushed_at = meta.get("pushed_at", "")
    now = datetime.now(timezone.utc)

    repo_age_days = 0
    days_since_push = 0
    try:
        if created_at:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            repo_age_days = (now - created_dt).days
        if pushed_at:
            pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            days_since_push = (now - pushed_dt).days
    except (ValueError, TypeError):
        pass

    bus_factor = sum(1 for c in contrib_list if c["percentage"] > 10)

    # Red flags (deterministic rules)
    red_flags: list[dict] = []
    if days_since_push > 90:
        red_flags.append({
            "category": "activity",
            "description": f"No commits for {days_since_push} days",
            "severity": "high",
        })
    elif days_since_push > 30:
        red_flags.append({
            "category": "activity",
            "description": f"No commits for {days_since_push} days",
            "severity": "medium",
        })
    if bus_factor <= 1 and len(contrib_list) <= 1:
        red_flags.append({
            "category": "team",
            "description": "Single contributor",
            "severity": "medium",
        })
    if not has_tests:
        red_flags.append({
            "category": "quality",
            "description": "No tests",
            "severity": "medium",
        })
    if not has_readme:
        red_flags.append({
            "category": "quality",
            "description": "No README",
            "severity": "high",
        })
    if not meta.get("license"):
        red_flags.append({
            "category": "legal",
            "description": "No license",
            "severity": "low",
        })
    if meta.get("fork"):
        red_flags.append({
            "category": "originality",
            "description": "Fork",
            "severity": "medium",
        })
    if repo_age_days < 14:
        red_flags.append({
            "category": "timeline",
            "description": f"Repo created {repo_age_days} days ago",
            "severity": "high",
        })
    if total_commits_count < 10:
        red_flags.append({
            "category": "scope",
            "description": f"Few commits ({total_commits_count})",
            "severity": "low",
        })

    # Health score
    health = 50
    if days_since_push < 7:
        health += 20
    elif days_since_push < 30:
        health += 10
    elif days_since_push > 90:
        health -= 10
    if bus_factor >= 3:
        health += 15
    elif bus_factor >= 2:
        health += 10
    elif bus_factor >= 1:
        health += 5
    if has_tests:
        health += 5
    if has_ci:
        health += 5
    if has_docker:
        health += 3
    if has_readme:
        health += 2
    if total_commits_count > 50:
        health += 5
    health = max(0, min(100, health))

    return {
        "owner": owner,
        "repo": repo,
        "full_name": f"{owner}/{repo}",
        "default_branch": default_branch,
        "stars": meta.get("stargazers_count", 0),
        "forks_count": meta.get("forks_count", 0),
        "open_issues": meta.get("open_issues_count", 0),
        "is_fork": meta.get("fork", False),
        "license": (meta.get("license") or {}).get("spdx_id"),
        "created_at": created_at,
        "pushed_at": pushed_at,
        "primary_language": meta.get("language"),
        "languages": languages if isinstance(languages, dict) else {},
        "topics": (topics or {}).get("names", []) if isinstance(topics, dict) else [],
        "total_commits": total_commits_count,
        "contributors_count": len(contrib_list),
        "contributors": contrib_list,
        "recent_commits": recent_commits,
        "file_tree_sample": file_tree[:50],
        "has_tests": has_tests,
        "has_ci": has_ci,
        "has_docker": has_docker,
        "has_readme": has_readme,
        "has_requirements": has_requirements,
        "repo_age_days": repo_age_days,
        "days_since_last_push": days_since_push,
        "bus_factor": bus_factor,
        "health_score": health,
        "red_flags": red_flags,
        "analyzed_at": now.isoformat(),
    }


# --- Real-time drill-down functions ---


async def fetch_file(owner: str, repo: str, path: str, token: str = "") -> str:
    """Fetch file content via gh api."""
    data = await gh_api(f"repos/{owner}/{repo}/contents/{path}", token)
    if not data or not isinstance(data, dict):
        return f"File {path} not found in {owner}/{repo}"

    if data.get("type") == "dir":
        return f"{path} is a directory. Use query_type='tree'"

    if data.get("size", 0) > 500_000:
        return f"File too large ({data['size'] // 1024}KB)"

    content_b64 = data.get("content", "")
    try:
        content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        return "Cannot decode file (binary?)"

    if len(content) > MAX_FILE_CHARS:
        return content[:MAX_FILE_CHARS] + f"\n\n... (truncated, total {len(content)} chars)"
    return content


async def fetch_tree(owner: str, repo: str, token: str = "", path: str = "") -> str:
    """Fetch file tree."""
    branch = "main"
    meta = await gh_api(f"repos/{owner}/{repo}", token)
    if meta and isinstance(meta, dict):
        branch = meta.get("default_branch", "main")

    endpoint = f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    data = await gh_api(endpoint, token)
    if not data or not isinstance(data, dict):
        # fallback to master
        data = await gh_api(f"repos/{owner}/{repo}/git/trees/master?recursive=1", token)
    if not data or not isinstance(data, dict):
        return f"Cannot fetch tree for {owner}/{repo}"

    entries = data.get("tree", [])

    # Filter by path prefix if given
    if path:
        path = path.rstrip("/")
        entries = [
            e for e in entries
            if e.get("path", "").startswith(path + "/") or e.get("path", "") == path
        ]

    lines = [f"Structure {owner}/{repo}" + (f"/{path}" if path else "") + ":\n"]
    for item in entries[:100]:
        p = item.get("path", "")
        t = item.get("type", "")
        suffix = "/" if t == "tree" else ""
        lines.append(f"  {p}{suffix}")

    if len(entries) > 100:
        lines.append(f"\n  ... and {len(entries) - 100} more files")

    return "\n".join(lines)


async def fetch_commits(owner: str, repo: str, token: str = "", author: str = "") -> str:
    """Fetch recent commits."""
    endpoint = f"repos/{owner}/{repo}/commits?per_page=10"
    if author:
        endpoint += f"&author={author}"

    data = await gh_api(endpoint, token)
    if not data or not isinstance(data, list):
        return f"Cannot fetch commits for {owner}/{repo}"

    lines = [f"Commits {owner}/{repo}" + (f" (author: {author})" if author else "") + ":\n"]
    for c in data[:10]:
        commit = c.get("commit", {})
        author_data = commit.get("author", {})
        sha = c.get("sha", "")[:7]
        commit_date = author_data.get("date", "")[:10]
        name = author_data.get("name", "?")
        msg = commit.get("message", "").split("\n")[0][:80]
        lines.append(f"  {sha} | {commit_date} | {name} | {msg}")

    return "\n".join(lines)


async def fetch_contributors(owner: str, repo: str, token: str = "") -> str:
    """Fetch contributors."""
    data = await gh_api(f"repos/{owner}/{repo}/contributors?per_page=20", token)
    if not data or not isinstance(data, list):
        return f"Cannot fetch contributors for {owner}/{repo}"

    total = sum(c.get("contributions", 0) for c in data)
    lines = [f"Contributors {owner}/{repo}:\n"]
    for c in data[:10]:
        login = c.get("login", "?")
        contribs = c.get("contributions", 0)
        pct = (contribs / total * 100) if total > 0 else 0
        lines.append(f"  {login}: {contribs} commits ({pct:.0f}%)")

    return "\n".join(lines)
