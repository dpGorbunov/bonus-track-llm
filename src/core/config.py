from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Bot
    bot_token: str
    bot_mode: str = "polling"  # polling or webhook

    # Platform
    platform_url: str = "http://localhost:8000"
    master_token: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://eventai:eventai@localhost:5432/eventai"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""

    # LLM
    llm_model: str = "deepseek/deepseek-v3.2"
    embedding_model: str = "google/gemini-embedding-001"
    openrouter_api_key: str = ""  # for standalone mode (no llm-agent-platform)

    # GitHub
    github_token: str = ""  # GitHub token for API access via gh CLI

    # Organizer
    organizer_chat_id: int = 0

    # Limits
    rate_limit_per_minute: int = 10
    semaphore_limit: int = 10
    agent_timeout: float = 45.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
