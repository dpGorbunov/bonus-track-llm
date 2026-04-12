# CLAUDE.md - EventAI Agent

## Проект

EventAI Agent - Telegram-бот AI-куратор Demo Day. Помогает гостям находить проекты через диалоговое профилирование и рекомендации.

## Стек

- Python 3.12, aiogram 3.x, PydanticAI, SQLAlchemy 2.0 (asyncpg), pgvector, Redis
- LLM через llm-agent-platform (OpenRouter proxy): deepseek/deepseek-v3.2
- Docker Compose: bot + postgres (pgvector) + redis

## Пайплайн разработки фич (ОБЯЗАТЕЛЬНО)

При реализации любой новой фичи или группы фич - строго по порядку:

### 1. Brainstorming (скилл superpowers:brainstorming)
- Изучить контекст (код, доки, существующие тесты)
- Задать уточняющие вопросы (по одному)
- Предложить 2-3 подхода с trade-offs
- Презентовать дизайн по секциям, получить одобрение
- Записать spec в `docs/superpowers/specs/`

### 2. Реализация
- Писать код по spec
- Коммитить атомарно (одна фича = один коммит)

### 3. Тесты
- Прогнать ВСЕ существующие тесты: `python3.12 -m pytest tests/ --tb=short -q`
- Написать новые тесты на фичу
- Coverage check: `python3.12 -m pytest tests/ --cov=src --cov-report=term-missing`
- Все тесты должны быть зелеными перед коммитом

### 4. Интерактивное тестирование агентами
- Запустить 3 агента с разными персонами через `scripts/chat.py --session=<name>`
- Каждый агент общается интерактивно (по одному сообщению, читает ответ, решает следующий шаг)
- Персоны: студент, бизнес/HR, chaos tester
- Собрать фидбэк, исправить найденные баги
- Повторить тесты после исправлений

### 5. Коммит и пуш
- Только после зеленых тестов и прохождения агентного тестирования

## Команды

```bash
# Тесты
BOT_TOKEN=test DATABASE_URL="postgresql+asyncpg://eventai:eventai@localhost:5432/eventai" REDIS_URL="redis://:testpassword@localhost:6379/0" REDIS_PASSWORD=testpassword python3.12 -m pytest tests/ --tb=short -q

# Coverage
BOT_TOKEN=test DATABASE_URL="postgresql+asyncpg://eventai:eventai@localhost:5432/eventai" REDIS_URL="redis://:testpassword@localhost:6379/0" REDIS_PASSWORD=testpassword python3.12 -m pytest tests/ --cov=src --cov-report=term-missing

# Интерактивный CLI (для человека)
OPENROUTER_API_KEY=<key> python3.12 scripts/cli_bot.py

# Интерактивный чат для агентов (изолированные сессии)
OPENROUTER_API_KEY=<key> python3.12 scripts/chat.py --session=<name> "<сообщение>"
OPENROUTER_API_KEY=<key> python3.12 scripts/chat.py --session=<name> "@callback_data"
OPENROUTER_API_KEY=<key> python3.12 scripts/chat.py --session=<name> "!state"
OPENROUTER_API_KEY=<key> python3.12 scripts/chat.py --session=<name> "!reset"

# Docker
docker compose up -d postgres redis
docker compose exec postgres psql -U eventai -d eventai

# Очистка данных
docker compose exec postgres psql -U eventai -d eventai -c "TRUNCATE expert_scores, experts, business_followups, support_log, recommendations, chat_messages, guest_profiles, users CASCADE;"
```

## Структура

```
src/
  main.py              # entrypoint
  core/                # config, database, sanitize, telegram_format
  models/              # SQLAlchemy (13 таблиц)
  schemas/             # Pydantic (ComparisonMatrix, ProjectExtraction)
  bot/
    states.py          # 8 FSM states
    routers/           # start, profiling, program, detail, expert, support, fallback
    middlewares/       # db, platform, throttle, reconcile
    keyboards/         # roles, program, expert
  agent/               # PydanticAI (agent.py, tools.py)
  services/            # platform_client, retriever, profiling, expert, support
  prompts/             # agent, profiling, qa
scripts/
  schema.sql           # DDL (13 таблиц, pgvector)
  seed.sql             # roles
  cli_bot.py           # интерактивный CLI для человека
  chat.py              # stateful chat для агентов
tests/                 # 128 тестов, 60% coverage
```

## Ключевые решения

- aiogram 3.x + PydanticAI (single-turn, 5 tools) + asyncio (без Celery)
- pgvector вместо Qdrant (330 проектов, brute-force cosine <1ms)
- Redis: FSM state + rate limiting (не Celery broker)
- telegramify-markdown: LLM markdown -> Telegram entities (без parse_mode)
- Fallback router последним в dispatcher (глобальные /help, /support, catch-all)
- sanitize_text() на всех DB writes (null byte protection)
