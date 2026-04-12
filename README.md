# EventAI Agent - AI-куратор Demo Day

Telegram-бот с AI-агентом для навигации по Demo Day AI Talent Hub (ИТМО). Профилирование через диалог, персональные рекомендации проектов, инструменты агента.

## Система EventAI

| Репо | Назначение | Стек |
|------|-----------|------|
| **[eventai-agent](https://github.com/demoday-ai/eventai-agent)** (этот репо) | AI-агент, Telegram-бот | aiogram 3.x, PydanticAI, pgvector, Redis |
| [eventai-platform](https://github.com/demoday-ai/eventai-platform) | Основная платформа, админка | FastAPI, React, PostgreSQL, Celery |
| [llm-agent-platform](https://github.com/demoday-ai/llm-agent-platform) | LLM-инфраструктура | FastAPI, Prometheus, Grafana, Langfuse |

Агент использует llm-agent-platform как LLM proxy (routing, circuit breaker, guardrails, cost tracking). Данные проектов загружаются из eventai-platform или через import-скрипт.

## Задача

Demo Day AI Talent Hub (ИТМО) - 330 проектов, 10 залов, 2 дня. Проблемы:

- Гость видит ~16% программы и выбирает вслепую
- Эксперты приходят без информации, оценки собираются в таблицах
- Вопросы участников летят в личку организатору

## Что делает агент

- **Профилирование:** LLM уточняет роль/интересы/цели за 2-3 реплики
- **Рекомендации:** embedding (Gemini 3072d) -> pgvector cosine search -> schedule-aware rerank -> персональная программа с расписанием
- **7 инструментов:** карточка проекта (по номеру или имени), сравнение 2-5 проектов, Q&A вопросы, фильтрация по тегам, follow-up/pipeline, смена статуса pipeline, итоги
- **Эксперты:** оценки проектов 1-5 по критериям
- **PDF экспорт:** программа в PDF с контактами авторов
- **Деградация:** LLM недоступна -> tag overlap scoring, timeout -> fallback

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/demoday-ai/eventai-agent.git
cd eventai-agent

# 2. Настроить
cp .env.example .env
# Заполнить BOT_TOKEN, OPENROUTER_API_KEY (или PLATFORM_URL)

# 3. Запустить
docker compose up -d

# 4. Тестирование в терминале
OPENROUTER_API_KEY=<key> python3.12 scripts/cli_bot.py
```

## Архитектура

```
aiogram 3.x (Telegram transport, FSM 8 states)
  PydanticAI Agent (single-turn, 7 tools)
  telegramify-markdown (LLM output -> Telegram entities)
PostgreSQL + pgvector (13 tables, cosine search)
Redis (FSM state, rate limiting)
LLM через llm-agent-platform или напрямую OpenRouter
```

## Стек

- Python 3.12, aiogram 3.x, PydanticAI
- SQLAlchemy 2.0 (asyncpg), pgvector
- Redis, Docker Compose
- DeepSeek V3.2 (default LLM), Gemini Embedding 001
- fpdf2 (PDF), telegramify-markdown (Telegram formatting)

## Тесты

```bash
# 141 тест, 60% coverage
python3.12 -m pytest tests/ --tb=short -q

# Интерактивный CLI для ручного тестирования
OPENROUTER_API_KEY=<key> python3.12 scripts/cli_bot.py

# Stateful chat для автоматизированного тестирования (изолированные сессии)
OPENROUTER_API_KEY=<key> python3.12 scripts/chat.py --session=<name> "<сообщение>"
```

## Документация

- [Product Proposal](docs/product-proposal.md) - обоснование, метрики, сценарии
- [System Design](docs/system-design.md) - архитектура, workflow, failure modes
- [Governance](docs/governance.md) - риски, PII, injection protection
- [Agent Service Spec](docs/superpowers/specs/2026-04-11-eventai-agent-service-design.md) - детальная спецификация
- [Diagrams](docs/diagrams/) - C4 Context/Container/Component, Workflow, Data Flow

## За рамками PoC

- Организация встреч 1:1 (только запрос контакта автора)
- Самостоятельная загрузка артефактов студентами
- Автоматическая фильтрация персональных данных
- Локальная LLM
