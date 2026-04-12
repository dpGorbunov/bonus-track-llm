# Design Spec: EventAI Agent Service

## Контекст

Реализация EventAI Agent как независимого сервиса. Агент использует llm-agent-platform (отдельный репо) как LLM proxy с мониторингом, circuit breaker и guardrails.

Код агента в demoday-core используется как референс для бизнес-логики.

## Архитектура

```
bonus-track-llm (EventAI Agent)
  aiogram 3.x (transport, FSM, 8 states)
    PydanticAI Agent (single-turn, 7 tools)
    Commands: /start, /help, /support, /profile, /rebuild
  Services
    PlatformClient -> llm-agent-platform
      /v1/chat/completions
      /v1/embeddings
    Retriever (pgvector search, schedule rerank)
  Storage
    PostgreSQL 16 + pgvector (13 tables)
    Redis (FSM state, rate limiting)
  Docker
    3 сервиса: bot, postgres, redis

llm-agent-platform (отдельный репо)
  /v1/chat/completions (existing)
  /v1/embeddings (new endpoint)
  Model routing, circuit breaker, guardrails
  Prometheus + Grafana, Langfuse
```

## Ключевые решения

| Решение | Выбор | Причина |
|---------|-------|---------|
| Telegram framework | aiogram 3.x | Native async, FSM, DI, middleware, MIT |
| Agent engine | PydanticAI (single-turn) | Type-safe tools, DI, model-agnostic, testable (TestModel) |
| LLM proxy | llm-agent-platform | Единый мониторинг, circuit breaker, guardrails, cost tracking |
| Embeddings | Через платформу | Единый мониторинг, не дублировать API keys |
| Database | PostgreSQL + pgvector | pgvector для 330 проектов - brute-force cosine <1ms. Без отдельного Qdrant |
| Schema init | schema.sql + seed.sql | Надежнее create_all(), проще Alembic. Идемпотентный (IF NOT EXISTS). Фиксированная схема |
| FSM storage | Redis | Переживает restart |
| Async | asyncio + semaphore + wait_for | Без Celery. Все I/O-bound, async покрывает 200 users |

### Что НЕ используем и почему

| Отвергнуто | Причина |
|-----------|---------|
| Celery + RabbitMQ | 200 users, все LLM-вызовы I/O-bound. asyncio + semaphore(10) + wait_for(timeout) проще и достаточно |
| Qdrant | 330 проектов * 768d = 1MB. pgvector в том же PostgreSQL. Минус контейнер, 256MB RAM |
| Alembic | Overhead для PoC. schema.sql - фиксированная схема, запускается один раз |
| ReAct multi-turn | 7 tools, single-turn. Multi-turn = лишний latency и cost |
| Walking penalty | Marginal improvement. conflict_penalty + room_bonus покрывают 95% |
| Expert slot selection | Организатор назначает зал при invite |

---

## State Machine (8 состояний)

```python
class BotStates(StatesGroup):
    choose_role = State()
    onboard_nl_profile = State()
    onboard_confirm = State()
    view_program = State()          # agent mode + tools
    view_detail = State()
    support_chat = State()
    expert_dashboard = State()
    expert_evaluation = State()
```

### Таблица переходов

| Из | Триггер | В | Условие |
|----|---------|---|---------|
| - | `/start` (новый user) | choose_role | - |
| - | `/start` (returning guest с профилем) | view_program | profile exists в БД |
| - | `/start?expert=<code>` | expert_dashboard | valid invite code |
| - | `/start` (returning expert) | expert_dashboard | expert.bot_started |
| choose_role | кнопка роли | onboard_nl_profile | - |
| choose_role | "Показать все проекты" | view_program | shortcut: все проекты по популярности, без профилирования |
| onboard_nl_profile | LLM: action=profile | onboard_confirm | profile extracted |
| onboard_nl_profile | LLM: action=reply (< 2 replies) | onboard_nl_profile | continue |
| onboard_confirm | "Все верно" | view_program | triggers recommendation gen |
| onboard_confirm | "Заново" | onboard_nl_profile | clears profile |
| view_program | user text | view_program | PydanticAI agent -> response |
| view_program | agent: show_project() | view_detail | - |
| view_program | `/rebuild` | onboard_nl_profile | clears history, profile |
| view_program | `/support` | support_chat | confirmation message first |
| view_detail | "Назад" | view_program | - |
| support_chat | user text | support_chat | forward to organizer group (rate: 3/5min, max 1000 chars) |
| support_chat | "Назад к программе" / timeout 30min | view_program | - |
| expert_dashboard | "Оценить проект N" | expert_evaluation | - |
| expert_evaluation | оценки + "Подтвердить" | expert_dashboard | saves scores |
| expert_evaluation | "Назад" | expert_dashboard | discards |
| * | `/start` | choose_role | full reset |

### Разрешенные действия по состояниям

| Состояние | Текст | Кнопки | Команды | Agent tools |
|-----------|-------|--------|---------|-------------|
| choose_role | игнор | роль, "Показать все проекты" | /start, /help | - |
| onboard_nl_profile | -> LLM profiling | - | /start | - |
| onboard_confirm | игнор | "Все верно", "Заново" | /start | - |
| view_program | -> PydanticAI Agent | "Профиль", "Если успеете" | /start, /help, /support, /profile, /rebuild | show_project, show_profile, compare_projects, generate_questions, get_summary |
| view_detail | игнор | "Назад", "Вопросы к проекту" | /start | - |
| support_chat | -> forward to group | "Назад к программе" | /start | - |
| expert_dashboard | игнор | "Оценить проект N" | /start | - |
| expert_evaluation | комментарий | score 1-5, "Подтвердить", "Назад" | /start | - |

### Re-entry

| Сценарий | Поведение |
|----------|-----------|
| Guest с профилем | -> view_program (recommendations из БД) |
| Guest без профиля | -> choose_role |
| Business на следующий день | -> view_program (profile + pipeline из БД) |
| Expert, зал назначен | -> expert_dashboard (проекты + progress) |
| Expert, часть оценок сдана | -> expert_dashboard ("Оценено: 3/7") |

### State reconciliation

Source of truth: **PostgreSQL**. Redis FSM = кэш.

При входе в handler middleware проверяет:
- Redis state есть, PostgreSQL data consistent -> proceed
- Redis state есть, PostgreSQL data missing (e.g. profile deleted) -> пересчитать state из БД
- Redis state нет -> восстановить из БД (profile? expert? -> соответствующий state)
- Конфликт (Redis = expert_evaluation, но expert record deleted) -> choose_role + сообщение "Сессия сброшена"

Конкретно: `ReconcileMiddleware` проверяет consistency только при:
- `/start` (всегда)
- Отсутствии Redis state (FSM state = None)
- НЕ на каждый update (лишний round-trip к БД)

---

## PydanticAI Agent (single-turn)

### Разделение

```
aiogram: транспорт (Telegram, FSM, keyboards, commands, /rebuild, /support)
PydanticAI: reasoning (один LLM-вызов, tool selection, response)
```

FSM transitions -> aiogram commands. Информационные запросы -> PydanticAI.

### 7 tools

```python
@agent.tool
async def show_project(ctx: RunContext[AgentDeps], project_rank: int) -> str:
    """Показать детали проекта по номеру в рекомендациях."""
    # Pre-formatted Markdown. Включает structured extraction из артефактов.

@agent.tool
async def show_profile(ctx: RunContext[AgentDeps]) -> str:
    """Показать текущий профиль (теги, интересы, цели)."""

@agent.tool
async def compare_projects(ctx: RunContext[AgentDeps], project_ranks: list[int]) -> ComparisonMatrix:
    """Сравнить 2-5 проектов. Матрица сравнения по критериям."""
    # Structured output - LLM форматирует для пользователя

@agent.tool
async def generate_questions(ctx: RunContext[AgentDeps], project_rank: int) -> list[str]:
    """Подготовить 3-5 вопросов для Q&A к проекту."""

@agent.tool
async def get_summary(ctx: RunContext[AgentDeps]) -> str:
    """Итоги. Гости: follow-up (контакты + шаблон). Бизнес: pipeline (статусы + контакты + шаблоны для связи).
    Адаптируется под роль автоматически."""

@agent.tool
async def update_status(ctx: RunContext[AgentDeps], project_rank: int, status: str) -> str:
    """Обновить статус проекта в пайплайне. Только для бизнес-партнеров.
    Статусы: interested, contacted, meeting_scheduled, rejected, in_progress."""

@agent.tool
async def filter_projects(ctx: RunContext[AgentDeps], tag: str) -> str:
    """Отфильтровать рекомендованные проекты по тегу или технологии."""
```

### Structured vs pre-formatted

| Tool | Return | Причина |
|------|--------|---------|
| show_project | `str` (Markdown) | Фиксированный формат, LLM не добавляет ценности |
| show_profile | `str` (Markdown) | Фиксированный формат |
| compare_projects | `ComparisonMatrix` | LLM генерирует матрицу (один вызов). PydanticAI получает structured result и форматирует в текст для Telegram (не второй LLM-вызов, а шаблонное форматирование в handler) |
| generate_questions | `list[str]` | Список, форматируется в handler |
| get_summary | `str` (Markdown) | Фиксированный формат (pipeline с контактами + шаблоны для бизнеса) |
| update_status | `str` | Бизнес-only. Upsert BusinessFollowup |
| filter_projects | `str` | Case-insensitive поиск по tags и tech_stack в рекомендациях |

### Error handling

| Тип | Пример | Поведение |
|-----|--------|-----------|
| UserError | "Проект #99 не найден" | LLM формулирует сообщение пользователю |
| SystemError | LLM platform down, DB timeout | Прервать agent run. "Бот временно не может обработать запрос. Команды /profile и кнопки работают." |

SystemError не передается LLM для retry.

### Dependencies (DI)

```python
@dataclass
class AgentDeps:
    platform: PlatformClient
    db: AsyncSession
    user: User
    profile: GuestProfile
    recommendations: list[Recommendation]
    event: Event
```

### Context budget

| Компонент | Токены | Примечание |
|-----------|--------|------------|
| System prompt | ~500 | |
| Tool schemas (7 tools) | ~2100 | update_status + filter_projects добавлены |
| Profile info | ~200 | |
| Recommendations (top-15, без summaries) | ~1500 | title + tags + room + time. Без LLM summaries |
| Chat history (до 20 msg) | ~3000 | tool results: краткая выжимка (title + tags), не полный результат. Хранится в PostgreSQL (chat_messages table), не в Redis |
| User message | ~100 | max 2000 символов (обрезаем) |
| **Итого worst case** | **~6800** | |

### Invocation из aiogram

```python
@router.message(BotStates.view_program)
async def handle_agent_message(message: Message, state: FSMContext,
                                db: AsyncSession, platform: PlatformClient):
    data = await state.get_data()
    deps = AgentDeps(platform=platform, db=db, ...)

    try:
        result = await asyncio.wait_for(
            agent.run(message.text, deps=deps, message_history=data.get("chat_history", [])),
            timeout=settings.agent_timeout  # default 45.0
        )
    except (asyncio.TimeoutError, Exception):
        await message.answer("Бот временно не может обработать запрос. Команды /profile и кнопки работают.")
        return

    history = result.all_messages()[-20:]
    await state.update_data(chat_history=history)
    await message.answer(result.data, parse_mode="Markdown")
```

---

## Schedule-Aware Recommendations

### Pipeline

1. **Embed** profile -> PlatformClient.embedding()
2. **pgvector** cosine search top-30: `ORDER BY embedding <=> $1 LIMIT 30 WHERE event_id = $2`
3. **Filter past slots**: исключить проекты с `start_time < now()` (MSK)
4. **Schedule-aware rerank**:
   - Greedy slot assignment по relevance_score
   - **Room bonus** (+3.0): проект в том же зале, что и предыдущий
   - **Slot conflict** (-inf): проект в занятом слоте -> исключить
5. **Формирование программы**:
   - `must_visit` (до 8): без пересечений, порядок по времени
   - `if_time` (до 7): запасные варианты
6. **Fallback**: tag overlap scoring (case-insensitive `lower()`) при недоступности embeddings
7. **Padding** до 10 результатов

### LLM summaries - lazy, не на онбординге

Summaries НЕ генерируются при онбординге (экономия 5-15 секунд). Генерируются лениво:
- При вызове `show_project(rank)` - summary для одного проекта
- Кэшируются в Redis (TTL 1 час)
- Если summary не в кэше и LLM timeout (>5s) - отдать карточку без summary с пометкой "Подробное описание загружается..." (summary догенерируется в background и закэшируется)

Рекомендации выдаются быстро (1-3 секунды: embed + pgvector + rerank):

```
Ваша программа:

10:00-10:20 | Зал 3 | #1 ChatLaw
  Теги: NLP, LLM, юриспруденция

10:20-10:40 | Зал 3 | #2 SentimentScope
  Теги: NLP, анализ тональности

11:00-11:20 | Зал 5 | #3 MedVision
  Теги: CV, медицина

Если успеете:
- 14:00 Зал 2 | #9 DataPipe (ETL, потоковая обработка)
- 15:00 Зал 4 | #10 RoboNav (робототехника, SLAM)

Напишите номер проекта, чтобы узнать подробности.
```

### Async execution

```python
_semaphore = asyncio.Semaphore(10)  # max 10 concurrent recommendation gens

async def generate_recommendations(deps, profile_text):
    async with _semaphore:
        embedding = await deps.platform.embedding(profile_text)
        candidates = await pgvector_search(deps.db, embedding, event_id, limit=30)
        candidates = filter_past_slots(candidates)
        ranked = schedule_rerank(candidates, schedule_slots)
        await save_recommendations(deps.db, ranked)
        return ranked
```

Timeout 15 секунд. При timeout -> fallback на tag overlap.

---

## Artifact Parser

### Structured extraction при импорте (не truncate)

```python
# scripts/parse_artifacts.py
async def parse_and_extract(project: Project):
    raw_text = await parse_pptx_or_pdf(project.presentation_url)  # или parse_github

    # LLM structured extraction
    extraction = await platform.chat_completion(
        messages=[{
            "role": "user",
            "content": f"Извлеки из текста презентации проекта:\n{raw_text[:5000]}"
        }],
        response_format=ProjectExtraction,  # Pydantic model
    )
    project.parsed_content = extraction.json()
```

```python
class ProjectExtraction(BaseModel):
    problem: str        # какую проблему решает (1-2 предложения)
    solution: str       # как решает (1-2 предложения)
    audience: str       # для кого
    stack: list[str]    # технологический стек
    novelty: str        # что нового
    risks: str | None   # ограничения, риски
```

- Запускается один раз при импорте
- `parsed_content` - JSON в поле Project, не отдельная таблица
- Tools используют structured fields, не raw truncated text
- Качество compare_projects и generate_questions значительно выше, чем при truncate

---

## Support (ask_organizer)

### Реализация: Telegram group forward с correlation_id

```
1. User нажимает /support
2. Bot: "Вы переходите в чат с организатором.
   Все сообщения будут переданы организатору.
   Нажмите [Назад к программе] чтобы вернуться."
3. FSM -> support_chat
4. User пишет вопрос (max 1000 chars, rate: 3 msg/5min)
5. Bot forwards в ORGANIZER_CHAT_ID:
   "[SQ-{short_id}] Вопрос от @username:
   {text}"
6. Организатор отвечает, начиная с "[SQ-{short_id}]"
7. Bot парсит correlation_id из текста, находит user, пересылает ответ
8. User может задать уточнение или нажать "Назад к программе"
```

Primary: `reply_to_message_id` (Telegram native). Fallback: correlation_id `[SQ-a1b2c3]` в тексте (если организатор не использует reply).

Лог: `support_log(user_id, event_id, question, answer, correlation_id, created_at, answered_at)`.

---

## Expert Flow

### Упрощенная модель

Организатор назначает зал при invite. Эксперт не выбирает слоты.

### Flow

```
1. /start?expert=<invite_code>  (secrets.token_urlsafe(16), 22+ chars)
   -> проверить invite_code (rate limit: 3 неудачных за 5 мин -> block 1 час)
   -> expert.bot_started = True
   -> FSM -> expert_dashboard

2. expert_dashboard:
   "Ваш зал: Зал 3 - NLP проекты
   Проектов: 7, оценено: 3/7

   1. [x] ChatLaw (4.2)
   2. [x] SentimentScope (3.8)
   3. [ ] DocParser
   ...
   [Оценить проект 3] [Оценить проект 4] ..."

3. "Оценить проект 3" -> expert_evaluation:
   "DocParser - Парсер юридических документов

   Техническая сложность: [1] [2] [3] [4] [5]
   Практическая применимость: [1-5]
   Качество презентации: [1-5]
   Инновационность: [1-5]
   Командная работа: [1-5]

   Комментарий: (напишите текстом)
   [Подтвердить] [Назад]"

4. "Подтвердить":
   -> Проверить project.room_id == expert.room_id
   -> INSERT ... ON CONFLICT (expert_id, project_id) DO UPDATE
   -> удалить inline keyboard
   -> FSM -> expert_dashboard
   -> "Оценка сохранена. Оценено: 4/7"
```

---

## Data Model (13 таблиц)

Schema init: `scripts/schema.sql` + `scripts/seed.sql`. Запускается один раз при deploy.

Tables: events, projects, rooms, schedule_slots, roles (5 read-only) + users, guest_profiles, recommendations, chat_messages (4 user/profile) + experts, expert_scores (2 expert) + support_log, business_followups (2 support/business) = 13.

### Core (5 read-only)

```python
class Event(Base):
    __tablename__ = "events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str]
    start_date: Mapped[date]
    end_date: Mapped[date]
    description: Mapped[str | None]
    evaluation_criteria: Mapped[dict | None]  # JSONB, default: 5 criteria
    timezone: Mapped[str] = mapped_column(default="Europe/Moscow")
    is_active: Mapped[bool] = mapped_column(default=True)  # явный флаг вместо date-based selection

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    title: Mapped[str]
    description: Mapped[str]
    author: Mapped[str | None]
    telegram_contact: Mapped[str | None]
    track: Mapped[str | None]
    tags: Mapped[list[str] | None]              # JSONB
    tech_stack: Mapped[list[str] | None]
    github_url: Mapped[str | None]
    presentation_url: Mapped[str | None]
    parsed_content: Mapped[dict | None]          # JSONB: ProjectExtraction
    embedding: Mapped[list[float] | None]        # pgvector Vector(768)

class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str]
    display_order: Mapped[int]

class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    room_id: Mapped[UUID] = mapped_column(ForeignKey("rooms.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    start_time: Mapped[datetime] = mapped_column(index=True)
    end_time: Mapped[datetime]
    day_number: Mapped[int]
    __table_args__ = (UniqueConstraint("room_id", "start_time"),)

class Role(Base):
    __tablename__ = "roles"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
```

### User + Profile (3 таблицы)

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    telegram_user_id: Mapped[str] = mapped_column(unique=True, index=True)
    full_name: Mapped[str]
    username: Mapped[str | None]
    role_code: Mapped[str | None] = mapped_column(Enum("guest", "business", "expert", name="role_enum"))
    subrole: Mapped[str | None]    # student, applicant, other, investor, hr
    created_at: Mapped[datetime] = mapped_column(default=func.now())

class GuestProfile(Base):
    __tablename__ = "guest_profiles"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    selected_tags: Mapped[list[str] | None]
    keywords: Mapped[list[str] | None]
    raw_text: Mapped[str | None]
    nl_summary: Mapped[str | None]
    company: Mapped[str | None]          # business role
    position: Mapped[str | None]         # business role
    objective: Mapped[str | None]        # investment, hiring, technology, partnership
    business_objectives: Mapped[list[str] | None]
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())

class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("guest_profiles.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    relevance_score: Mapped[float]
    category: Mapped[str]       # must_visit, if_time
    rank: Mapped[int]
    slot_id: Mapped[UUID | None] = mapped_column(ForeignKey("schedule_slots.id"))
    visit_order: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    __table_args__ = (UniqueConstraint("profile_id", "project_id"),)
    # При /rebuild: DELETE FROM recommendations WHERE profile_id = $1, затем INSERT
```

### Chat Messages (новая таблица, chat_history в PostgreSQL)

```python
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    role: Mapped[str]          # user, assistant, tool_result
    content: Mapped[str]       # текст сообщения или краткая выжимка tool result (title + tags)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    # При загрузке: SELECT ... WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20
```

### Expert (2 таблицы)

```python
class Expert(Base):
    __tablename__ = "experts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    invite_code: Mapped[str] = mapped_column(unique=True, index=True)  # secrets.token_urlsafe(16)
    name: Mapped[str]
    room_id: Mapped[UUID | None] = mapped_column(ForeignKey("rooms.id"))
    tags: Mapped[list[str] | None]
    bot_started: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

class ExpertScore(Base):
    __tablename__ = "expert_scores"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    expert_id: Mapped[UUID] = mapped_column(ForeignKey("experts.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    criteria_scores: Mapped[dict]    # JSONB: {"Техническая сложность": 4, ...} (1-5)
    comment: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())
    __table_args__ = (UniqueConstraint("expert_id", "project_id"),)
```

### Support + Business (2 таблицы)

```python
class SupportLog(Base):
    __tablename__ = "support_log"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    correlation_id: Mapped[str] = mapped_column(unique=True, index=True)  # SQ-{short_id}
    question: Mapped[str]
    answer: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    answered_at: Mapped[datetime | None]

class BusinessFollowup(Base):
    __tablename__ = "business_followups"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    status: Mapped[str] = mapped_column(default="interested")
    notes: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())
```

---

## PlatformClient

```python
class PlatformClient:
    async def register(self) -> str          # POST /agents -> token (SecretStr)
    async def chat_completion(...) -> dict    # POST /v1/chat/completions
    async def embedding(text, model) -> list[float]  # POST /v1/embeddings
```

- Auto-reregistration при 401 (max 3 за 5 мин)
- Retry: tenacity, 3 attempts, backoff 1s/2s/4s
- Token хранится как SecretStr, исключен из __repr__ и логов
- Graceful degradation: при platform down show_project/show_profile работают из БД

---

## Concurrency (200 пользователей)

### Per-user lock + rate limiting

```python
class ThrottleMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        lock_value = str(uuid4())

        # Rate limit
        count = await redis.incr(f"rate:min:{user_id}")
        if count == 1:
            await redis.expire(f"rate:min:{user_id}", 60)
        if count > 10:
            await event.answer("Слишком много сообщений. Подождите минуту.")
            return

        # Per-user mutex via SET NX (single Redis, не Redlock)
        lock = await redis.set(f"lock:{user_id}", lock_value, ex=60, nx=True)
        if not lock:
            await event.answer("Подождите, обрабатываю предыдущий запрос...")
            return
        try:
            return await handler(event, data)
        finally:
            await redis.eval(
                "if redis.call('get',KEYS[1])==ARGV[1] then redis.call('del',KEYS[1]) end",
                1, f"lock:{user_id}", lock_value
            )
```

### asyncio model

Event loop обрабатывает все 200 users. LLM-вызовы - async I/O (httpx), не блокируют.

```python
_semaphore = asyncio.Semaphore(10)  # max 10 concurrent LLM-heavy tasks

async def llm_with_limit(coro):
    try:
        await asyncio.wait_for(_semaphore.acquire(), timeout=10.0)  # timeout на ожидание в очереди
    except asyncio.TimeoutError:
        raise SystemError("Слишком много запросов, попробуйте через минуту")
    try:
        return await asyncio.wait_for(coro, timeout=settings.agent_timeout  # default 45.0)
    finally:
        _semaphore.release()
```

### Health endpoint

aiogram webhook mode использует aiohttp. В polling mode - минимальный aiohttp server на отдельном порту:

```python
async def health_handler(request):
    await redis.ping()
    await db.execute(text("SELECT 1"))
    return web.json_response({"status": "ok"})
```

### Graceful shutdown

```python
async def on_shutdown(app):
    # Перестать принимать новые updates
    await dp.stop_polling()
    # Дождаться текущих handlers (max 15s)
    await asyncio.sleep(15)
    # Закрыть connections
    await platform.close()
    await db_engine.dispose()
```

SIGTERM handler в main.py. Docker stop_timeout: 20s (> 15s graceful wait).

---

## Docker Compose

```yaml
services:
  bot:
    build: .
    env_file: .env
    ports: ["8080:8080"]
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    stop_grace_period: 30s
    healthcheck:
      test: curl -f http://localhost:8080/health
      interval: 10s
      retries: 3
    deploy:
      resources:
        limits: { memory: 256M }
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "3" }

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: eventai
      POSTGRES_USER: eventai
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
      - ./scripts/seed.sql:/docker-entrypoint-initdb.d/02-seed.sql
    healthcheck:
      test: pg_isready -U eventai
      interval: 5s
    command: postgres -c shared_buffers=64MB -c work_mem=4MB
    deploy:
      resources:
        limits: { memory: 256M }

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 100mb --maxmemory-policy allkeys-lru --save "" --requirepass ${REDIS_PASSWORD}
    healthcheck:
      test: redis-cli -a ${REDIS_PASSWORD} ping
      interval: 5s
    deploy:
      resources:
        limits: { memory: 128M }

volumes:
  pgdata:
```

**Total RAM: ~640MB (3 сервиса).** На VM 4GB - 3.4GB свободно.

---

## Структура проекта

```
bonus-track-llm/
  docs/
  src/
    bot/
      routers/
        start.py        # /start, choose_role, re-entry
        profiling.py    # onboard_nl_profile, onboard_confirm
        program.py      # view_program (PydanticAI agent)
        detail.py       # view_detail
        expert.py       # expert_dashboard, expert_evaluation
        support.py      # support_chat (group forward + correlation_id)
      middlewares/
        db.py           # async session
        platform.py     # PlatformClient
        throttle.py     # Redlock + rate limiting
        reconcile.py    # FSM/DB state reconciliation
      keyboards/
    agent/
      agent.py          # PydanticAI Agent (single-turn, 7 tools)
      tools.py          # tool implementations
      platform_model.py # PydanticAI Model adapter
      prompts.py        # system prompt builder
    services/
      platform_client.py
      profiling.py
      retriever.py      # embed -> pgvector -> schedule rerank
      support.py
      expert.py
      followup.py
    models/             # SQLAlchemy (13 tables)
    schemas/            # Pydantic
    prompts/            # LLM prompt templates
    core/
      config.py
      database.py          # async engine
      sanitize.py          # null-byte removal for DB writes
      telegram_format.py   # telegramify-markdown -> entities
    main.py
  services/
    pdf_export.py          # fpdf2 PDF generation with DejaVu fonts
  scripts/
    schema.sql             # DDL + pgvector extension
    seed.sql               # events, roles
    cli_bot.py             # interactive CLI for manual testing
    chat.py                # stateful chat for agent testing (--session=)
    import_projects.py     # CSV/JSON -> projects + rooms + schedule
    parse_artifacts.py     # PPTX/PDF/GitHub -> structured extraction
  fonts/
    DejaVuSans.ttf         # Cyrillic font for PDF
    DejaVuSans-Bold.ttf
  tests/
  Dockerfile
  docker-compose.yml
  pyproject.toml
  .env.example
```

## Зависимости

```
aiogram >= 3.27
pydantic-ai >= 0.2
sqlalchemy[asyncio] >= 2.0
asyncpg
httpx
pgvector
redis[hiredis]
tenacity
python-pptx
pymupdf
pydantic-settings
aiohttp                    # health endpoint
telegramify-markdown>=1.1  # LLM markdown -> Telegram entities
fpdf2>=2.8                 # PDF export with Cyrillic (DejaVu fonts)
```

## Implementation Decisions

Решения по пробелам, выявленным на ревью.

### PlatformModel adapter

llm-agent-platform предоставляет OpenAI-совместимый API (`/v1/chat/completions`). Использовать встроенный `OpenAIModel` из PydanticAI:

```python
from pydantic_ai.models.openai import OpenAIModel

platform_model = OpenAIModel(
    model_name="deepseek/deepseek-v3.2",
    base_url=f"{PLATFORM_URL}/v1",
    api_key=agent_token,  # полученный при register()
)
agent = Agent(model=platform_model, ...)
```

Кастомный adapter не нужен. Auto-reregistration при 401: обернуть в middleware PlatformClient, который перехватывает 401, вызывает register(), обновляет api_key.

### `/start` - smart re-entry, не full reset

```
/start:
  1. Проверить deep link (?expert=<code>) -> expert_dashboard
  2. Проверить БД: profile exists? -> view_program
  3. Проверить БД: expert exists, bot_started? -> expert_dashboard
  4. Ничего -> choose_role
```

`/start` НЕ удаляет данные. Полный сброс = `/rebuild` (удаляет profile + recommendations).

### Shortcut "Показать все проекты"

```
choose_role -> "Показать все проекты":
  1. Создать User (role_code=null, subrole=null)
  2. НЕ создавать GuestProfile
  3. Загрузить все проекты по display_order (без personalization)
  4. AgentDeps.profile = None, AgentDeps.recommendations = all projects
  5. Tools работают: show_project (по rank в общем списке), show_profile ("Профиль не создан. Используйте /rebuild для персонализации")
  6. compare_projects, generate_questions - работают (по project_id)
  7. get_summary - "Создайте профиль через /rebuild для персонального follow-up"
```

### view_detail: "Вопросы к проекту"

Прямой вызов `platform.chat_completion()`, не через PydanticAI agent:

```python
@router.callback_query(BotStates.view_detail, F.data.startswith("questions:"))
async def handle_questions(callback: CallbackQuery, platform: PlatformClient):
    project = get_current_project(callback)
    questions = await asyncio.wait_for(
        generate_questions_direct(platform, project, user),
        timeout=settings.agent_timeout  # default 45.0
    )
    await callback.message.answer(format_questions(questions))
```

### Organizer response handler

Отдельный router для сообщений из группы организаторов:

```python
# bot/routers/organizer_group.py
group_router = Router()

@group_router.message(F.chat.id == ORGANIZER_CHAT_ID)
async def handle_organizer_reply(message: Message):
    # Парсим correlation_id из текста: "[SQ-a1b2c3] ответ..."
    match = re.match(r"\[SQ-(\w+)\]", message.text)
    if not match:
        return
    correlation_id = f"SQ-{match.group(1)}"
    log = await get_support_log(correlation_id)
    if log and log.answer is None:  # проверяем что ответа еще не было
        await bot.send_message(log.user_telegram_id, f"Ответ организатора:\n{message.text}")
        log.answer = message.text
        log.answered_at = datetime.now()
        await save(log)
```

Фильтр: `F.chat.id == ORGANIZER_CHAT_ID`. Роутер регистрируется в main.py вне FSM.

### Event selection

Active event: `SELECT * FROM events WHERE is_active = true LIMIT 1`. Явный флаг вместо date-based selection. Кэшируется при старте бота.

### /help и /profile

- `/help` - статическое сообщение, зависит от role_code: guest видит список tools, expert видит инструкцию по оценке
- `/profile` - вызывает `show_profile` tool напрямую (без PydanticAI agent), показывает текущий профиль

### ComparisonMatrix

```python
class ComparisonMatrix(BaseModel):
    projects: list[str]                         # ["ChatLaw", "SentimentScope"]
    criteria: list[str]                         # ["Стек", "Применимость", ...]
    matrix: dict[str, dict[str, str]]           # {"ChatLaw": {"Стек": "GPT-4, LangChain", ...}}
```

Определяется в `src/schemas/tools.py`.

### Model names

Конфигурация через .env:
```
LLM_MODEL=deepseek/deepseek-v3.2
EMBEDDING_MODEL=google/gemini-embedding-001
```

### Project embedding generation

`scripts/import_projects.py` после загрузки проектов вызывает `PlatformClient.embedding()` для каждого проекта и записывает в `projects.embedding`. Единый скрипт: import -> parse artifacts -> embed.

---

## Security

| Мера | Реализация |
|------|------------|
| Rate limiting | 10 msg/min, 50 msg/hour per user |
| Expert invite code | secrets.token_urlsafe(16). Brute-force: 3 fails/5min -> block 1h (по telegram_user_id) |
| User input length | max 2000 chars (обрезка с предупреждением: "Сообщение обрезано до 2000 символов") |
| Prompt injection | Смягчение (не полная защита): instruction markers в system prompt, input length limit. parsed_content фильтруется при import (structured extraction, не raw text) |
| Tool validation | Ranks строго из deps.recommendations |
| Expert room check | project.room_id == expert.room_id |
| Redis auth | requirepass |
| PlatformClient token | SecretStr, не в логах |

## Known Limitations

| Ограничение | Причина | Workaround |
|-------------|---------|------------|
| Single VM | PoC | Снапшот VM, pg_dump каждые 2ч |
| schema.sql (нет миграций) | PoC | pg_dump перед ALTER |
| pgvector для 330 проектов | При 1000+ проектах / параллельных мероприятиях: ivfflat/hnsw не поддерживают pre-filter по event_id. Brute-force замедлится | Partitioning по event_id или миграция на Qdrant |
| Pipeline read-only | Нет tool для смены статуса бизнес-воронки | Post-PoC |
| Prompt injection - смягчение | Полная защита невозможна | Structured extraction при import снижает risk от артефактов |
| Semaphore без FIFO | asyncio.Semaphore не гарантирует порядок. Под нагрузкой один user может ждать дольше других | Для 200 users приемлемо |
| Embedding asymmetry | Проекты и профили индексируются разным форматом. Может ухудшить NDCG | Если NDCG < 0.7 - добавить HyDE (LLM-generated query) |
| Redis = SPOF | FSM + locks + rate limiting на одном Redis | Для PoC допустимо. Production: Redis Sentinel |
| LLM summaries lazy | Не видны в списке рекомендаций, только при открытии проекта | Trade-off: быстрый онбординг (1-3s) vs полнота информации |

## Тестирование

- **Unit**: pytest + pytest-asyncio
  - PydanticAI agent с TestModel (mock LLM)
  - Reranking (schedule_rerank, slot conflict, past slot filter)
  - Tool implementations (role checks, error handling)
  - Expert scoring (ON CONFLICT, room_id check, criteria validation)
- **Integration**: test DB (schema.sql in fixture)
  - FSM transitions
  - Support loop (mock Telegram group)
  - State reconciliation
- **Coverage**: 80%+
