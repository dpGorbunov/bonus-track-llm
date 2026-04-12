"""PydanticAI agent configuration for EventAI Demo Day curator.

Creates an agent that routes LLM calls through llm-agent-platform
and provides tool-based interaction for project exploration.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.event import Event
from src.models.guest_profile import GuestProfile
from src.models.recommendation import Recommendation
from src.models.user import User
from src.prompts.agent import build_agent_system_prompt
from src.services.platform_client import PlatformClient


@dataclass
class AgentDeps:
    """Runtime dependencies injected into every agent tool call."""

    platform: PlatformClient
    db: AsyncSession
    user: User
    profile: GuestProfile | None
    recommendations: list[Recommendation]
    event: Event
    support_history: list[str] | None = None


def create_agent(platform_url: str, agent_token: str) -> Agent[AgentDeps, str]:
    """Create and configure the PydanticAI agent.

    Args:
        platform_url: Base URL of llm-agent-platform.
        agent_token: Bearer token obtained after platform registration.

    Returns:
        Configured Agent instance with all tools registered.
    """
    provider = OpenAIProvider(
        base_url=f"{platform_url}/v1",
        api_key=agent_token,
    )
    from src.core.config import settings
    model = OpenAIModel(
        model_name=settings.llm_model,
        provider=provider,
    )

    agent = Agent(
        model=model,
        deps_type=AgentDeps,
        output_type=str,
        instructions=_build_system_prompt,
    )

    from src.agent.tools import register_tools

    register_tools(agent)

    return agent


async def _build_system_prompt(ctx: RunContext[AgentDeps]) -> str:
    """Dynamic system prompt builder called by PydanticAI before each run."""
    from sqlalchemy import select
    from src.models.project import Project

    deps = ctx.deps
    is_business = deps.user.role_code == "business"

    profile_info = (
        _format_profile(deps.profile) if deps.profile else "Профиль не создан"
    )

    # Load project details for recommendations context
    project_ids = [r.project_id for r in deps.recommendations]
    projects_map: dict = {}
    if project_ids:
        result = await deps.db.execute(
            select(Project).where(Project.id.in_(project_ids))
        )
        projects_map = {p.id: p for p in result.scalars().all()}

    recs_summary = _format_recommendations(deps.recommendations, projects_map)

    prompt = build_agent_system_prompt(
        is_business=is_business,
        profile_info=profile_info,
        recs_summary=recs_summary,
        num_recommendations=len(deps.recommendations),
    )

    # Inject support chat history if available
    if deps.support_history:
        support_text = "\n".join(deps.support_history)
        prompt += f"\n\nИСТОРИЯ ОБЩЕНИЯ С ОРГАНИЗАТОРОМ:\n{support_text}"

    return prompt


def _format_profile(profile: GuestProfile) -> str:
    """Format GuestProfile into human-readable block for the system prompt."""
    parts: list[str] = []
    if profile.selected_tags:
        parts.append(f"Теги: {', '.join(profile.selected_tags)}")
    if profile.keywords:
        parts.append(f"Ключевые слова: {', '.join(profile.keywords)}")
    if profile.company:
        parts.append(f"Компания: {profile.company}")
    if profile.position:
        parts.append(f"Должность: {profile.position}")
    if profile.business_objectives:
        parts.append(f"Бизнес-цели: {', '.join(profile.business_objectives)}")
    if profile.nl_summary:
        parts.append(f"О пользователе: {profile.nl_summary}")
    return "\n".join(parts) if parts else "Нет данных"


def _format_recommendations(recs: list[Recommendation], projects: dict | None = None) -> str:
    """Format recommendation list with project details for the system prompt."""
    if not recs:
        return "Нет рекомендаций"

    projects = projects or {}
    lines: list[str] = []
    for rec in recs:
        project = projects.get(rec.project_id)
        if project:
            tags = ", ".join(project.tags[:3]) if project.tags else ""
            stack = ", ".join(project.tech_stack[:3]) if project.tech_stack else ""
            line = f"#{rec.rank} {project.title}"
            if tags:
                line += f" | теги: {tags}"
            if stack:
                line += f" | стек: {stack}"
            pc = project.parsed_content if isinstance(project.parsed_content, dict) else None
            if pc and pc.get("problem"):
                line += f" | проблема: {pc['problem'][:80]}"
            lines.append(line)
        else:
            lines.append(f"#{rec.rank}")
    return "\n".join(lines)
