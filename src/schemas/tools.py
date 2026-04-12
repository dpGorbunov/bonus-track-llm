from pydantic import BaseModel


class ComparisonMatrix(BaseModel):
    projects: list[str]
    criteria: list[str]
    matrix: dict[str, dict[str, str]]


class RedFlag(BaseModel):
    category: str      # "metric", "team", "scope", "technical"
    description: str
    severity: str      # "low", "medium", "high"


class ProjectExtraction(BaseModel):
    # Core
    problem: str
    solution: str
    audience: str
    stack: list[str]
    novelty: str
    risks: str | None = None

    # Metrics
    key_metrics: list[str] | None = None        # ["F1=0.91", "94% accuracy"]
    production_readiness: str | None = None      # "prototype" | "mvp" | "production"

    # Team
    team_size: int | None = None

    # Red flags
    red_flags: list[RedFlag] | None = None
