# EventAI Agent - AI-куратор Demo Day

Telegram-бот с AI-агентом для навигации по Demo Day AI Talent Hub (ИТМО). Профилирование через диалог, персональные рекомендации проектов, анализ GitHub-репозиториев, инструменты агента.

## Система EventAI

| Репо | Назначение | Стек |
|------|-----------|------|
| **[eventai-agent](https://github.com/demoday-ai/eventai-agent)** (этот репо) | AI-агент, Telegram-бот | aiogram 3.x, PydanticAI, pgvector, Redis |
| [llm-agent-platform](https://github.com/demoday-ai/llm-agent-platform) | LLM-инфраструктура | FastAPI, Prometheus, Grafana, Langfuse |

Агент использует llm-agent-platform как LLM proxy: все chat completions и embeddings идут через платформу с метриками, трейсами, circuit breaker и guardrails.

### Развернутые сервисы (85.198.96.191)

| Сервис | URL |
|--------|-----|
| Telegram-бот | [@demoday_ai_talent_hub_test_bot](https://t.me/demoday_ai_talent_hub_test_bot) |
| LLM Platform API | http://85.198.96.191:8000 |
| Grafana (метрики) | http://85.198.96.191:3002 (admin/admin) |
| Prometheus | http://85.198.96.191:9090 |
| Langfuse (трейсы) | http://85.198.96.191:3001 |
| Bot health | http://85.198.96.191:8080/health |

## Задача

Demo Day AI Talent Hub (ИТМО) - 330 проектов, 10 залов, 2 дня. Проблемы:

- Гость видит ~16% программы и выбирает вслепую
- Эксперты приходят без информации, оценки собираются в таблицах
- Вопросы участников летят в личку организатору

## Что делает агент

- **Профилирование:** LLM уточняет роль/интересы/цели за 2-3 реплики, force extraction на 3-м ходу
- **Рекомендации:** embedding (Gemini 3072d) -> pgvector cosine search -> schedule-aware rerank -> персональная программа с расписанием по залам
- **8 инструментов агента:**
  - `show_project` - карточка проекта с данными из артефактов (PPTX/PDF/README)
  - `show_profile` - текущий профиль гостя
  - `compare_projects` - LLM-матрица сравнения 2-5 проектов
  - `generate_questions` - персонализированные Q&A вопросы для автора
  - `filter_projects` - фильтрация по тегу или технологии
  - `get_summary` - follow-up пакет (контакты + шаблон) или бизнес-пайплайн
  - `update_status` - статус проекта в бизнес-пайплайне
  - `github_drilldown` - анализ GitHub-репо: метрики, файлы, структура, коммиты, контрибьюторы (live через gh CLI)
- **GitHub анализ:** cross-reference кода с описанием проекта, health score, red flags, drill-down в любой файл
- **Артефакты:** парсинг PPTX (python-pptx), PDF (pymupdf), GitHub README -> LLM structured extraction
- **Эксперты:** оценки проектов 1-5 по критериям мероприятия
- **PDF экспорт:** программа в PDF с контактами авторов (fpdf2 + DejaVu)
- **Поддержка:** пересылка вопросов организатору с tracking ID
- **Деградация:** LLM недоступна -> tag overlap scoring, timeout -> fallback

## Архитектура

```
                         docker network: eventai-net

  llm-agent-platform                          eventai-agent
  +--------------------------+                +---------------------------+
  | app:8000                 |  LLM calls     | bot:8080                  |
  |   /v1/chat/completions   |<---------------|   aiogram 3.x (FSM)       |
  |   /v1/embeddings         |                |   PydanticAI (8 tools)    |
  |   circuit breaker        |                |   telegramify-markdown    |
  |   guardrails             |                +---------------------------+
  +--------------------------+                | postgres:5432             |
  | prometheus:9090          |                |   pgvector (13 tables)    |
  | grafana:3000             |                | redis:6379                |
  | langfuse:3001            |                |   FSM state, rate limit   |
  +--------------------------+                +---------------------------+
              |
              v
       OpenRouter API
    (DeepSeek V3.2, Gemini)
```

## Observability

Все LLM-вызовы (chat + embeddings) проходят через llm-agent-platform:

| Слой | Что отслеживается |
|------|------------------|
| **Prometheus** | llm_requests_total, llm_embedding_requests_total, duration, tokens_in/out, cost |
| **Grafana** | Дашборд: latency p50/p95, traffic distribution, cost per model, TTFT/TPOT, circuit breaker |
| **Langfuse** | Трейсы каждого LLM-вызова: input/output, tokens, cost, duration, provider |
| **OpenTelemetry** | X-Trace-Id в response headers, span per HTTP request |
| **Guardrails** | Prompt injection detection (user messages), secret leak masking (responses) |
| **Circuit breaker** | Per-provider: CLOSED -> OPEN (5 errors/60s) -> HALF_OPEN (probe) -> CLOSED |

## Быстрый старт

### С llm-agent-platform (рекомендуется)

```bash
# 1. Создать docker network
docker network create eventai-net

# 2. Запустить LLM платформу
git clone https://github.com/demoday-ai/llm-agent-platform.git
cd llm-agent-platform
cp .env.example .env
# Заполнить OPENROUTER_API_KEY и MASTER_TOKEN в .env
docker compose up -d
# Ждем: app healthy, prometheus:9090, grafana:3000, langfuse:3001

# 3. Запустить агента
cd ../eventai-agent
cp .env.example .env
# Заполнить BOT_TOKEN, PLATFORM_URL=http://app:8000, MASTER_TOKEN (тот же что в платформе)
docker compose up -d
# Бот доступен в Telegram
```

### Standalone (без платформы, без мониторинга)

```bash
git clone https://github.com/demoday-ai/eventai-agent.git
cd eventai-agent
cp .env.example .env
# Заполнить BOT_TOKEN и OPENROUTER_API_KEY (без MASTER_TOKEN)
docker compose up -d
```

## Стек

- Python 3.12, aiogram 3.x, PydanticAI
- SQLAlchemy 2.0 (asyncpg), pgvector (3072d Gemini embeddings)
- Redis 7, Docker Compose
- DeepSeek V3.2 (LLM), google/gemini-embedding-001 (embeddings)
- gh CLI (GitHub analysis), fpdf2 (PDF), telegramify-markdown
- python-pptx, pymupdf (artifact parsing)

## Тесты

```bash
# 202 теста
BOT_TOKEN=test python3.12 -m pytest tests/ --tb=short -q

# Coverage
BOT_TOKEN=test python3.12 -m pytest tests/ --cov=src --cov-report=term-missing

# Интерактивный CLI (standalone)
OPENROUTER_API_KEY=<key> python3.12 scripts/cli_bot.py

# Интерактивный CLI (platform mode)
PLATFORM_URL=http://localhost:8000 MASTER_TOKEN=<token> python3.12 scripts/cli_bot.py

# Stateful chat для автоматизированного тестирования
python3.12 scripts/chat.py --session=<name> "<сообщение>"
```

## Структура

```
src/
  main.py              # entrypoint, auto-seed, auto-embed, health :8080
  core/                # config, database, sanitize, telegram_format
  models/              # SQLAlchemy (13 tables, pgvector)
  schemas/             # Pydantic (ComparisonMatrix, ProjectExtraction)
  bot/
    states.py          # 8 FSM states
    routers/           # start, profiling, program, detail, expert, support, fallback
    middlewares/       # db, platform, throttle, reconcile
    keyboards/         # roles, program, expert
  agent/               # PydanticAI (agent.py, tools.py - 8 tools)
  services/            # platform_client, retriever, profiling, expert, support,
                       # github_analyzer, artifact_parser, pdf_export
  prompts/             # agent, profiling, qa
scripts/
  schema.sql           # DDL (13 tables, pgvector, indexes)
  seed.sql             # demo data (7 projects, 3 rooms, 7 slots)
  cli_bot.py           # interactive CLI (human testing)
  chat.py              # stateful chat (agent testing, isolated sessions)
  parse_artifacts.py   # batch PPTX/PDF/README parsing
tests/                 # 202 tests
```

## Документация

- [Product Proposal](docs/product-proposal.md) - обоснование, метрики, сценарии
- [System Design](docs/system-design.md) - архитектура, workflow, failure modes
- [Governance](docs/governance.md) - риски, PII, injection protection
- [Agent Service Spec](docs/superpowers/specs/2026-04-11-eventai-agent-service-design.md) - детальная спецификация
- [Diagrams](docs/diagrams/) - C4 Context/Container/Component, Data Flow

## За рамками PoC

- Интеграция с eventai-platform (общая БД, удаление встроенного бота)
- Организация встреч 1:1 (только запрос контакта автора)
- Загрузка артефактов студентами через бота
- Локальная LLM
