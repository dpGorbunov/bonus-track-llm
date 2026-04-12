"""Tool implementations for the EventAI PydanticAI agent.

Seven tools:
- show_project     -- show details of one recommended project (by rank or name)
- show_profile     -- show current user profile
- compare_projects -- compare 2-5 projects via LLM-generated matrix
- generate_questions -- generate Q&A questions for a project
- update_status    -- update project status in business pipeline
- filter_projects  -- filter recommended projects by tag or technology
- get_summary      -- follow-up (guest) or pipeline (business)
"""

import asyncio
import json
import logging

from pydantic_ai import Agent, RunContext
from sqlalchemy import select, func

from src.agent.agent import AgentDeps
from src.models.business_followup import BusinessFollowup
from src.models.project import Project
from src.models.recommendation import Recommendation

logger = logging.getLogger(__name__)


def register_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register all 7 tools on the given agent instance."""

    @agent.tool
    async def show_project(
        ctx: RunContext[AgentDeps], project_identifier: str
    ) -> str:
        """Показать детали проекта по номеру или названию."""
        deps = ctx.deps

        # Try rank first
        rec = None
        try:
            rank = int(project_identifier.strip().lstrip("#"))
            rec = _find_recommendation(deps.recommendations, rank)
        except ValueError:
            pass

        # Fallback: search by name among recommended projects
        if not rec:
            name_lower = project_identifier.strip().lower()
            project_ids = [r.project_id for r in deps.recommendations]
            if project_ids:
                result = await deps.db.execute(
                    select(Project).where(
                        Project.id.in_(project_ids),
                        func.lower(Project.title).contains(name_lower),
                    )
                )
                matched = result.scalars().first()
                if matched:
                    rec = next(
                        (r for r in deps.recommendations if r.project_id == matched.id),
                        None,
                    )

        if not rec:
            return f"Проект '{project_identifier}' не найден в рекомендациях."

        result = await deps.db.execute(
            select(Project).where(Project.id == rec.project_id)
        )
        project = result.scalar_one_or_none()
        if not project:
            return f"Проект '{project_identifier}' не найден."

        return _format_project_card(project, rec)

    @agent.tool
    async def show_profile(ctx: RunContext[AgentDeps]) -> str:
        """Показать текущий профиль (теги, интересы, цели)."""
        deps = ctx.deps
        if not deps.profile:
            return "Профиль не создан. Используйте /rebuild для персонализации."

        from src.agent.agent import _format_profile

        return _format_profile(deps.profile)

    @agent.tool
    async def compare_projects(
        ctx: RunContext[AgentDeps], project_ranks: list[int]
    ) -> str:
        """Сравнить 2-5 проектов. Матрица сравнения по критериям."""
        deps = ctx.deps
        if len(project_ranks) < 2:
            return "Для сравнения нужно минимум 2 проекта."

        ranks = project_ranks[:5]  # cap at 5

        projects: list[Project] = []
        for rank in ranks:
            rec = _find_recommendation(deps.recommendations, rank)
            if not rec:
                return f"Проект #{rank} не найден в рекомендациях."
            result = await deps.db.execute(
                select(Project).where(Project.id == rec.project_id)
            )
            project = result.scalar_one_or_none()
            if project:
                projects.append(project)

        if len(projects) < 2:
            return "Недостаточно проектов для сравнения."

        from src.prompts.qa import build_comparison_matrix_prompt

        is_business = deps.user.role_code == "business"
        criteria = _get_default_criteria(is_business)
        projects_text = "\n".join(
            f"- {p.title}: {p.description[:200]}. Стек: {', '.join(p.tech_stack or [])}"
            for p in projects
        )

        system_prompt, user_prompt = build_comparison_matrix_prompt(
            projects_text, criteria
        )

        try:
            resp = await asyncio.wait_for(
                deps.platform.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                ),
                timeout=25.0,
            )
            content = resp["choices"][0]["message"]["content"]
            matrix_data = json.loads(content)
            return _format_matrix(matrix_data.get("matrix", {}), criteria)
        except (asyncio.TimeoutError, json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error("Compare projects failed: %s", e)
            return "Не удалось сгенерировать сравнение. Попробуйте позже."

    @agent.tool
    async def generate_questions(
        ctx: RunContext[AgentDeps], project_rank: int
    ) -> str:
        """Подготовить 3-5 вопросов для Q&A к проекту."""
        deps = ctx.deps
        rec = _find_recommendation(deps.recommendations, project_rank)
        if not rec:
            return f"Проект #{project_rank} не найден в рекомендациях."

        result = await deps.db.execute(
            select(Project).where(Project.id == rec.project_id)
        )
        project = result.scalar_one_or_none()
        if not project:
            return "Проект не найден."

        from src.prompts.qa import build_business_qa_prompt, build_guest_qa_prompt

        if deps.user.role_code == "business":
            system_prompt, user_prompt = build_business_qa_prompt(
                objective=(
                    deps.profile.objective if deps.profile else "technology"
                ),
                industries=(
                    ", ".join(deps.profile.business_objectives or [])
                    if deps.profile
                    else ""
                ),
                tech_stack=", ".join(project.tech_stack or []),
                project_title=project.title,
                project_description=project.description[:500],
                project_tech_stack=", ".join(project.tech_stack or []),
            )
        else:
            system_prompt, user_prompt = build_guest_qa_prompt(
                subtype=deps.user.subrole or "other",
                interests=(
                    ", ".join(deps.profile.selected_tags or [])
                    if deps.profile
                    else ""
                ),
                project_title=project.title,
                project_description=project.description[:500],
                project_tech_stack=", ".join(project.tech_stack or []),
            )

        try:
            resp = await asyncio.wait_for(
                deps.platform.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                ),
                timeout=20.0,
            )
            content = resp["choices"][0]["message"]["content"]
            data = json.loads(content)
            questions = data.get("questions", [])

            lines = [f"Вопросы для проекта #{project_rank} ({project.title}):\n"]
            for i, q in enumerate(questions, 1):
                lines.append(f"{i}. {q}")
            return "\n".join(lines)
        except (asyncio.TimeoutError, json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error("Generate questions failed: %s", e)
            return "Не удалось сгенерировать вопросы. Попробуйте позже."

    @agent.tool
    async def update_status(
        ctx: RunContext[AgentDeps], project_rank: int, status: str
    ) -> str:
        """Обновить статус проекта в пайплайне. Только для бизнес-партнеров.
        Допустимые статусы: interested, contacted, meeting_scheduled, rejected, in_progress."""
        deps = ctx.deps
        if deps.user.role_code != "business":
            return "Эта функция доступна только бизнес-пользователям."

        VALID = {"interested", "contacted", "meeting_scheduled", "rejected", "in_progress"}
        if status not in VALID:
            return f"Допустимые статусы: {', '.join(sorted(VALID))}"

        rec = _find_recommendation(deps.recommendations, project_rank)
        if not rec:
            return f"Проект #{project_rank} не найден."

        result = await deps.db.execute(
            select(BusinessFollowup).where(
                BusinessFollowup.user_id == deps.user.id,
                BusinessFollowup.event_id == deps.event.id,
                BusinessFollowup.project_id == rec.project_id,
            )
        )
        followup = result.scalar_one_or_none()
        if followup:
            old = followup.status
            followup.status = status
            await deps.db.flush()
            return f"Статус проекта #{project_rank} изменен: {old} -> {status}"
        else:
            new = BusinessFollowup(
                user_id=deps.user.id,
                event_id=deps.event.id,
                project_id=rec.project_id,
                status=status,
            )
            deps.db.add(new)
            await deps.db.flush()
            return f"Проект #{project_rank} добавлен в пайплайн: {status}"

    @agent.tool
    async def filter_projects(ctx: RunContext[AgentDeps], tag: str) -> str:
        """Отфильтровать рекомендованные проекты по тегу или технологии."""
        deps = ctx.deps
        if not deps.recommendations:
            return "Нет рекомендаций. Используйте /rebuild."

        tag_lower = tag.strip().lower()
        matched: list[tuple[Recommendation, Project]] = []

        # Load all recommended projects in one query
        project_ids = [r.project_id for r in deps.recommendations]
        result = await deps.db.execute(
            select(Project).where(Project.id.in_(project_ids))
        )
        projects = {p.id: p for p in result.scalars().all()}

        for rec in deps.recommendations:
            project = projects.get(rec.project_id)
            if not project:
                continue
            project_tags = [t.lower() for t in (project.tags or [])]
            project_stack = [t.lower() for t in (project.tech_stack or [])]
            if tag_lower in project_tags or tag_lower in project_stack:
                matched.append((rec, project))

        if not matched:
            return f"Нет проектов с тегом '{tag}' в ваших рекомендациях."

        lines = [f"Проекты с тегом '{tag}' ({len(matched)}):\n"]
        for rec, project in matched:
            lines.append(f"#{rec.rank} {project.title}")
            tags_str = ", ".join(project.tags[:3]) if project.tags else ""
            if tags_str:
                lines.append(f"   {tags_str}")
        return "\n".join(lines)

    @agent.tool
    async def get_summary(ctx: RunContext[AgentDeps]) -> str:
        """Итоги. Гости: follow-up (контакты + шаблон). Бизнес: pipeline (статусы)."""
        deps = ctx.deps
        if deps.user.role_code == "business":
            return await _get_pipeline(deps)
        return await _get_followup(deps)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_recommendation(
    recs: list[Recommendation], rank: int
) -> Recommendation | None:
    """Find recommendation by rank number."""
    for r in recs:
        if r.rank == rank:
            return r
    return None


def _get_default_criteria(is_business: bool) -> list[str]:
    """Return default comparison criteria based on user role."""
    if is_business:
        return [
            "Стадия проекта",
            "Размер команды",
            "Технический стек",
            "Бизнес-модель",
            "Готовность к пилоту",
        ]
    return [
        "Тематика",
        "Технологии",
        "Практическая применимость",
        "Инновационность",
        "Зрелость проекта",
    ]


def _format_project_card(project: Project, rec: Recommendation) -> str:
    """Format a single project into a readable card."""
    lines = [
        f"#{rec.rank} {project.title}\n",
        project.description[:300],
    ]
    if project.tags:
        lines.append(f"\nТеги: {', '.join(project.tags)}")
    if project.tech_stack:
        lines.append(f"Стек: {', '.join(project.tech_stack)}")

    if project.parsed_content and isinstance(project.parsed_content, dict):
        pc = project.parsed_content
        if pc.get("problem"):
            lines.append(f"\nПроблема: {pc['problem']}")
        if pc.get("solution"):
            lines.append(f"Решение: {pc['solution']}")
        if pc.get("novelty"):
            lines.append(f"Новизна: {pc['novelty']}")

    if project.author:
        lines.append(f"\nАвтор: {project.author}")
    return "\n".join(lines)


def _format_matrix(matrix: dict, criteria: list[str]) -> str:
    """Format comparison matrix dict into readable text."""
    if not matrix:
        return "Не удалось сгенерировать матрицу."

    lines = ["Матрица сравнения:\n"]
    for criterion in criteria:
        lines.append(f"*{criterion}:*")
        for project_name, scores in matrix.items():
            value = scores.get(criterion, "-")
            lines.append(f"  {project_name}: {value}")
        lines.append("")
    return "\n".join(lines)


async def _get_followup(deps: AgentDeps) -> str:
    """Build follow-up package for guest users."""
    if not deps.recommendations:
        return "Нет рекомендаций. Используйте /rebuild."

    lines = ["Follow-up пакет:\n"]
    for rec in deps.recommendations[:10]:
        result = await deps.db.execute(
            select(Project).where(Project.id == rec.project_id)
        )
        project = result.scalar_one_or_none()
        if project:
            contact = (
                f" | {project.telegram_contact}" if project.telegram_contact else ""
            )
            lines.append(f"#{rec.rank} {project.title}{contact}")

    lines.append("\nШаблон для связи:")
    lines.append("Здравствуйте! Видел(а) ваш проект на Demo Day.")
    lines.append("Интересует возможность сотрудничества.")
    return "\n".join(lines)


async def _get_pipeline(deps: AgentDeps) -> str:
    """Build business pipeline summary."""
    result = await deps.db.execute(
        select(BusinessFollowup).where(
            BusinessFollowup.user_id == deps.user.id,
            BusinessFollowup.event_id == deps.event.id,
        )
    )
    followups = result.scalars().all()

    if not followups:
        return "Пайплайн пуст. Сначала получите рекомендации."

    stats: dict[str, int] = {}
    for f in followups:
        stats[f.status] = stats.get(f.status, 0) + 1

    lines = ["Business Pipeline:\n"]
    for status, count in stats.items():
        lines.append(f"  {status}: {count}")
    lines.append("")

    for f in followups[:10]:
        result = await deps.db.execute(
            select(Project).where(Project.id == f.project_id)
        )
        project = result.scalar_one_or_none()
        if project:
            lines.append(f"[{f.status}] {project.title}")
            if project.telegram_contact:
                lines.append(f"  Контакт: {project.telegram_contact}")
            if f.notes:
                lines.append(f"  {f.notes[:50]}")

    company = deps.profile.company if deps.profile and deps.profile.company else "[название компании]"

    lines.append("\nШаблоны для связи:")
    lines.append("")
    lines.append("Первое обращение:")
    lines.append(f"Здравствуйте! Представляю компанию {company}.")
    lines.append("Видели ваш проект [название проекта] на Demo Day.")
    lines.append("Интересует обсуждение возможного сотрудничества.")
    lines.append("Удобно будет созвониться на этой неделе?")
    lines.append("")
    lines.append("Повторное обращение:")
    lines.append("Добрый день! Мы общались на Demo Day по проекту [название].")
    lines.append("Хотел(а) бы уточнить детали для запуска пилота.")

    return "\n".join(lines)
