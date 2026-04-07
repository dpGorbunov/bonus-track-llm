# System Design - EventAI Agent

Архитектурный документ агентской части системы EventAI.

Scope: Telegram-бот (AI-агент), взаимодействующий с бэкенд-сервисами. Веб-админка, фронтенд, инфраструктура оркестрации - внешние зависимости. Диаграммы - в `diagrams/`, детальные спецификации - в `specs/`.

Основной репозиторий: [demoday-ai/demoday-core](https://github.com/demoday-ai/demoday-core) (приватный).

---

## 1. Ключевые архитектурные решения

### ConversationHandler как конечный автомат

Бот реализован через `ConversationHandler` (python-telegram-bot 21.x) с 7 состояниями:

| # | Состояние | Назначение |
|---|-----------|------------|
| 0 | CHOOSE_ROLE | Выбор роли: студент, абитуриент, бизнес, другое |
| 1 | ONBOARD_NL_PROFILE | Диалоговое профилирование через LLM (макс. 2 уточняющих вопроса) |
| 2 | ONBOARD_CONFIRM | Подтверждение извлеченного профиля (теги, цели, контекст) |
| 3 | VIEW_PROGRAM | Генерация рекомендаций, показ программы, agent mode с инструментами |
| 4 | VIEW_DETAIL | Детальный просмотр проекта |
| 5 | NL_REBUILD | Перепрофилирование (возврат к NL-диалогу) |
| 6 | SUPPORT_CHAT | Вопрос организатору через админку |

Переход между состояниями - детерминированный (по кнопкам и командам). LLM работает внутри состояний, не управляет переходами.

### Celery для асинхронных LLM-операций

LLM-вызовы выполняются через Celery-задачи (брокер: RabbitMQ). Пользователь получает "typing..." индикатор, результат - по колбэку. Причины:

- LLM-вызов занимает 2-8 секунд; синхронное ожидание блокирует event loop бота
- Celery дает retry, таймауты и мониторинг (Flower) из коробки
- Один воркер обслуживает и бота, и API

Ключевая задача: `agent_chat_task` - отправка сообщения в LLM с инструментами, обработка ответа, запись в `user_data`.

### OpenRouter с multi-key rotation и fallback-моделью

| Параметр | Значение |
|----------|----------|
| Провайдер | OpenRouter (openrouter.ai) |
| Primary model | `openai/gpt-5.1` (GPT-5-Mini tier в OpenRouter) |
| Fallback model | `openai/gpt-4o-mini` |
| Ключи | Пул из N ключей, round-robin |
| Ротация | При 429/503 - следующий ключ, cooldown 60 с для заблокированного |
| Fallback | При 400/403/404 - переключение на fallback-модель |
| No-LLM degradation | При полном отказе - детерминированные ответы |

### Qdrant для векторного поиска

Qdrant хранит эмбеддинги проектов (768d, Gemini embedding через OpenRouter). Косинусная близость, top-30 кандидатов. Одна коллекция "projects" с фильтрацией по event_id. Fallback при недоступности - пересечение тегов.

---

## 2. Список модулей и их роли

```
+-----------------------------------------------------+
|                  Telegram Bot (PTB)                  |
|  ConversationHandler -> states -> callbacks          |
+-------+--------+--------+--------+---------+--------+
        |        |        |        |         |
   Profiler  Retriever  Agent   Tools   ArtifactParser  ExpertFlow
                       Orchestr.          (planned)     (planned)
```

| Модуль | Роль | Статус |
|--------|------|--------|
| **Profiler** | Диалоговое профилирование: LLM извлекает интересы/цели/роль из NL-диалога (макс. 2 уточняющих вопроса). Выход - структурированный JSON (interests, goals, context) | Реализован |
| **Retriever** | Рекомендательный пайплайн: embed профиля -> Qdrant top-30 -> schedule rerank -> LLM-резюме -> top-15 | Реализован |
| **Agent/Orchestrator** | Управление агентским диалогом: формирование контекста, отправка в LLM с инструментами (`send_chat_with_tools`), диспатч вызовов, запись истории | Реализован |
| **Tools** | 7 инструментов агента (show_project, compare_projects, generate_questions, get_followup, get_pipeline, rebuild_profile, show_profile). Каждый - отдельная функция с контрактом вход/выход | Реализован |
| **ArtifactParser** | Парсинг PPTX (python-pptx), PDF (pymupdf), GitHub (git clone --depth 1). Извлеченный текст -> parsed_pitch / parsed_repo в БД | **Planned** |
| **ExpertFlow** | Бот-хендлеры для экспертов: выбор слотов доступности, оценки проектов по критериям. Модели и API есть, бот-часть - нет | **Planned** |

---

## 3. Основной workflow выполнения задачи

Сценарий "Гость получает персональную программу":

```
/start
  |
  v
CHOOSE_ROLE -- кнопка роли --> ONBOARD_NL_PROFILE
  |                                    |
  |                            LLM: уточняющий вопрос (до 2 раз)
  |                                    |
  |                                    v
  |                            ONBOARD_CONFIRM
  |                            "Все верно" / "Заново"
  |                                    |
  |                                    v
  |                            VIEW_PROGRAM
  |                            embed(profile) -> Qdrant top-30
  |                            -> schedule_rerank -> LLM summaries
  |                            -> top-15, затем agent mode + tools
  |                                    |
  |                                    v
  |                            VIEW_DETAIL (при show_project)
  |                            NL_REBUILD (при rebuild_profile)
  |
  +--- /start в любом состоянии --> сброс, повтор с начала
```

**Ветки ошибок:**

| Этап | Ошибка | Поведение |
|------|--------|-----------|
| ONBOARD_NL_PROFILE | LLM timeout | Retry (3 попытки). При полном отказе - профилирование по кнопкам (без LLM) |
| VIEW_PROGRAM (рекомендации) | Qdrant недоступен | Fallback на tag overlap scoring |
| VIEW_PROGRAM (рекомендации) | LLM timeout (резюме) | Резюме = первые предложения описания проекта |
| VIEW_PROGRAM (agent mode) | Tool failure | Сообщение пользователю "Не удалось выполнить" + продолжение диалога |
| VIEW_PROGRAM (agent mode) | LLM timeout | Retry -> fallback model -> сообщение "Попробуйте позже" |

Детали workflow с ветками ошибок - `diagrams/workflow.md`.

---

## 4. State / memory / context handling

### Персистентность

- **PicklePersistence** (PTB): состояние ConversationHandler и `user_data` сохраняются на диск. Переживает перезапуск контейнера
- **PostgreSQL**: GuestProfile, BusinessProfile, ExpertProfile, Recommendation, ExpertRating - долговременное хранение
- **Восстановление**: `/start` в любом состоянии пересоздает сессию; профиль загружается из БД, если существует

### user_data (in-memory dict, per user)

| Ключ | Тип | Назначение |
|------|-----|------------|
| `profile_id` | int | ID профиля в PostgreSQL |
| `nl_conversation` | list[dict] | Диалог профилирования (role, content) |
| `extracted_profile` | dict | Извлеченный профиль: interests, goals, context |
| `recommendations` | list[dict] | Top-15 с резюме и метаданными |
| `program_chat` | list[dict] | История агентского диалога |
| `role` | str | Роль пользователя (student, applicant, business, other) |

### Управление контекстом LLM

- **chat_history**: максимум 20 сообщений в `program_chat`. Старые обрезаются FIFO
- **Системный промпт**: роль, профиль, список доступных инструментов. Не содержит PII
- **Артефакты** (при show_project / compare_projects): передаются как user message, не system - для защиты от injection
- **Context budget**: системный промпт (~400 токенов) + tool definitions (~1500) + профиль (~200) + рекомендации (~2000) + chat history (до ~4000 при 20 msg) + support_chat_history (~500) = ~8600 токенов в worst case. Лимит модели позволяет

Детали - `specs/memory-context.md`.

---

## 5. Retrieval-контур

Пайплайн состоит из 4 этапов:

### 5.1 Embedding

- Модель: Gemini embedding через OpenRouter Embeddings API
- Размерность: 768d
- Вход: конкатенация interests + goals + context из профиля
- Проекты проиндексированы заранее (название + теги + описание -> embedding)

### 5.2 Qdrant search

- Метрика: cosine similarity
- Количество кандидатов: top-30
- Фильтры: event_id (одна коллекция "projects", фильтрация при запросе)

### 5.3 Schedule rerank

Детерминированное переранжирование top-30 с учетом расписания:

```
score = qdrant_score + room_bonus + conflict_penalty

room_bonus   = +3.0  (если проект в зале, куда гость уже идет)
conflict_penalty = -2.0 * (room_count - 1)  (штраф за конфликты в расписании)
```

### 5.4 LLM summaries + padding

- Top-15 после rerank получают LLM-резюме (2-3 предложения: почему проект релевантен профилю)
- Показ: 8 обязательных + до 7 дополнительных

### Fallback chain

| Уровень | Условие | Поведение |
|---------|---------|-----------|
| 1 | Qdrant доступен | Полный пайплайн |
| 2 | Qdrant недоступен | Tag overlap scoring: `score = overlap * 20.0` |
| 3 | Менее 10 результатов | Padding до 10 популярными проектами |
| 4 | LLM недоступна (резюме) | Первые предложения описания вместо LLM-резюме |

Детали - `specs/retriever.md`.

---

## 6. Tool/API-интеграции

### Реализованные инструменты (7)

| Инструмент | Назначение | Таймаут | Роли |
|------------|------------|---------|------|
| `show_project` | Карточка проекта: описание, теги, зал, время, артефакты | default | все |
| `show_profile` | Показ извлеченного профиля пользователю | default | все |
| `compare_projects` | Матрица сравнения 2-5 проектов по критериям | 25 с | все |
| `generate_questions` | Подсказки для Q&A по содержимому проекта | 20 с | все |
| `rebuild_profile` | Перезапуск профилирования | default | все |
| `get_followup` | Пакет контактов автора проекта (с согласия) | default | гости |
| `get_pipeline` | Воронка: заинтересован -> связались -> переговоры | default | бизнес |

Таймауты Celery-задач (не инструменты, но влияют на UX):

| Задача | Таймаут |
|--------|---------|
| `agent_chat_task` | 15 с |
| `chat_for_profile_task` | 15 с |
| `generate_recommendations_task` | 40 с |

### Планируемые инструменты

| Инструмент | Назначение | Статус |
|------------|------------|--------|
| `ask_organizer` | Вопрос организатору через support chat | **Planned** (support chat реализован, но не как agent tool) |

### Механизм вызова

1. LLM получает описания инструментов в системном промпте (JSON schema)
2. LLM возвращает `tool_calls` в ответе (OpenAI function calling формат через OpenRouter)
3. `send_chat_with_tools` в Agent/Orchestrator парсит ответ
4. **Whitelist validation**: имя инструмента проверяется по белому списку
5. **Argument validation**: аргументы проверяются по типам и обязательным полям
6. **Role-dependent availability**: `get_followup` доступен только гостям, `get_pipeline` - только бизнесу
7. Результат инструмента добавляется в историю как tool response
8. LLM формирует ответ пользователю на основе результата

Детали контрактов и валидации - `specs/tools.md`.

---

## 7. Failure modes, fallback и guardrails

### LLM failures

| Ситуация | Поведение |
|----------|-----------|
| Timeout / network error | 3 retry с exponential backoff (`2^(attempt+1)` секунд: 2, 4, 8 с при attempt=0,1,2) |
| 429 (rate limit) / 503 (service unavailable) | Ротация на следующий ключ; cooldown 60 с для текущего |
| 400 / 403 / 404 (model error) | Переключение на fallback-модель (`gpt-4o-mini`) |
| Все ключи в cooldown + fallback недоступен | No-LLM degradation: детерминированные ответы, tag overlap вместо embedding |

### Qdrant failures

| Ситуация | Поведение |
|----------|-----------|
| Qdrant недоступен | Fallback на tag overlap scoring (`overlap * 20.0`) |
| Менее 10 результатов | Padding популярными проектами до 10 |

### Tool failures

| Ситуация | Поведение |
|----------|-----------|
| Таймаут инструмента | Сообщение пользователю, продолжение диалога |
| Неизвестный инструмент | Отклонение (whitelist), логирование |
| Невалидные аргументы | Отклонение, сообщение LLM об ошибке |

### Guardrails

| Мера | Реализация |
|------|------------|
| Белый список инструментов | Агент вызывает только описанные 7 функций |
| Валидация аргументов | Каждый вызов проверяется по типам и обязательным полям |
| Артефакты как user message | Содержимое презентаций/репозиториев - user message, не system (снижает эффективность injection) |
| Ограничение длины входа | Текст пользователя обрезается перед отправкой в LLM |
| Cap истории | Максимум 20 сообщений в chat_history (FIFO) |
| PII-минимизация | ФИО и username не отправляются в LLM by design. Из проектов - только название/теги/описание |
| Изоляция контекстов | Данные организатора не попадают в промпты агента |

### Открытые риски

| Риск | Статус |
|------|--------|
| Rate limiting (governance risk #9) | **Не реализовано** - бот уязвим к злоупотреблению |
| Автоматическая PII-фильтрация | **Не реализовано** - только by-design минимизация |
| Песочница для инструментов | **Не реализовано** |
| Фильтрация контента артефактов | **Не реализовано** |

---

## 8. Технические и операционные ограничения

### Latency targets

| Операция | Целевое (p95) |
|----------|---------------|
| Команды, кнопки (детерминированные) | < 1 с |
| AI-операции (профилирование, рекомендации, agent chat) | < 10 с |
| compare_projects (до 5 проектов, LLM-тяжелая) | < 25 с |
| generate_questions (LLM-тяжелая) | < 20 с |

### Cost

| Модель | Input | Output | Роль |
|--------|-------|--------|------|
| `openai/gpt-5.1` | ~$0.25/M | ~$2.0/M | Primary (GPT-5-Mini tier, цена через OpenRouter) |
| `openai/gpt-4o-mini` | $0.15/M | $0.60/M | Fallback |

Оценка: ~$1-2 на 200 гостей при типичном сценарии (профилирование + рекомендации + 3-5 сообщений в agent mode).

### Reliability

| Параметр | Целевое |
|----------|---------|
| Uptime в день DD (10:00-20:00) | 99.5% |
| RPO (оценки экспертов) | 0 (атомарная запись в PostgreSQL) |
| Recovery | /start пересоздает сессию, профиль из БД |

### Infrastructure

| Параметр | Значение |
|----------|----------|
| Платформа | Yandex Cloud |
| Ресурсы | 2 vCPU, 4 GB RAM, 30 GB SSD |
| Runtime | Docker (бот + FastAPI + Celery worker + PostgreSQL + Qdrant + RabbitMQ + Redis) |
| Мониторинг | Python logging (INFO), health-эндпоинты, Flower для Celery |

### Observability (текущее состояние)

Реализовано:
- Python logging (plain text, INFO level)
- Health-эндпоинты: `/health`, `/monitoring/llm/health`, `/monitoring/llm/stats`
- Flower для мониторинга Celery
- AdminAuditLog в БД
- Логирование токенов (model, tokens, latency, key_id) без содержимого промптов

Не реализовано:
- Structured logging (JSON)
- Sentry / error tracking
- Prometheus / Grafana
- Distributed tracing
- Cost tracking в БД

### Offline evals (качество рекомендаций)

| Метрика | Значение | Условия |
|---------|----------|---------|
| NDCG@15 | 0.82 | 10 профилей x 330 проектов, ручная разметка 0-3 |
| Precision@15 | 0.71 | Проверочный замер, не научный результат |
| Recall@15 | 0.78 | Нет бейзлайна, нет IAA |

Детали - `specs/observability-evals.md`.
