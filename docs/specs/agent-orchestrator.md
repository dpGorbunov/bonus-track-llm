# Agent Orchestrator - Спецификация

State machine, переходы, agent chat flow, retry/fallback, LLM quality control.

Код: `backend/app/bot/handlers/start.py`, `backend/app/bot/handlers/states.py`, `backend/app/prompts/bot/agent.py`, `backend/app/services/core/llm_client.py`.

---

## State machine

ConversationHandler (python-telegram-bot 21.x) с 7 состояниями.

```
CHOOSE_ROLE       = 0  # Выбор роли (4 кнопки)
ONBOARD_NL_PROFILE = 1  # NL-профилирование (свободный текст -> LLM)
ONBOARD_CONFIRM   = 2  # Подтверждение профиля
VIEW_PROGRAM      = 3  # Программа + agent mode
VIEW_DETAIL       = 4  # Детали проекта
NL_REBUILD        = 5  # Пересборка профиля из agent mode
SUPPORT_CHAT      = 6  # Чат с организатором
```

### Entry points

- `/start` -> `start_command()` -> маршрутизация по состоянию пользователя
- `/role` -> `start_command()` (алиас)

### Конфигурация

```python
ConversationHandler(
    per_message=False,
    allow_reentry=True,    # /start можно отправить из любого состояния
)
```

---

## Правила переходов

### CHOOSE_ROLE (0)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Callback `role:guest:student` | `role_chosen()` | ONBOARD_NL_PROFILE |
| Callback `role:guest:applicant` | `role_chosen()` | ONBOARD_NL_PROFILE |
| Callback `role:guest:other` | `role_chosen()` | ONBOARD_NL_PROFILE |
| Callback `role:business` | `role_chosen()` | ONBOARD_NL_PROFILE |

### ONBOARD_NL_PROFILE (1)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Свободный текст | `onb_nl_free_text()` | ONBOARD_NL_PROFILE (reply) или ONBOARD_CONFIRM (profile) |

Логика:
1. Текст добавляется в `nl_conversation`
2. Celery task `chat_for_profile_task` с timeout 15s
3. LLM возвращает `action: "reply"` -> остаемся, `action: "profile"` -> подтверждение
4. Guard: если только 1 user message и 0 assistant messages -> принудительный follow-up вопрос

### ONBOARD_CONFIRM (2)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Callback `onb_nlconf:yes` | `onb_confirm_profile_callback()` | VIEW_PROGRAM (через `_do_generate()`) |
| Callback `onb_nlconf:retry` | `onb_confirm_profile_callback()` | ONBOARD_NL_PROFILE (сброс nl_conversation) |

### VIEW_PROGRAM (3)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Свободный текст | `view_program_text()` | VIEW_PROGRAM (текст/tool), VIEW_DETAIL (show_project), NL_REBUILD (rebuild_profile) |
| Callback `pdetail:*` | `view_program_callback()` | VIEW_DETAIL |
| Callback `profile:update` | `view_program_callback()` | NL_REBUILD |
| Callback `prof:show_profile` | `view_program_callback()` | VIEW_PROGRAM |
| Callback `prof:show_if_time` | `view_program_callback()` | VIEW_PROGRAM |
| Callback `prof:check_ready` | `view_program_callback()` | VIEW_PROGRAM |
| Callback `prof:retry_gen` | `view_program_callback()` | VIEW_PROGRAM |
| Callback `support:start` | `support_start_callback()` | SUPPORT_CHAT |

### VIEW_DETAIL (4)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Callback `qa:prep:*` | `qa_prep_callback()` | VIEW_DETAIL |
| Callback `qa:more:*` | `qa_more_callback()` | VIEW_DETAIL |
| Callback `pdetail:*` | `view_program_callback()` | VIEW_DETAIL |
| Callback `prof:back_program` | `back_to_program_callback()` | VIEW_PROGRAM |
| Callback `contact:req:*` | `contact_request_callback()` | VIEW_DETAIL |

### NL_REBUILD (5)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Свободный текст | `nl_rebuild_text()` | NL_REBUILD (reply) или ONBOARD_CONFIRM (profile) |

### SUPPORT_CHAT (6)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| Свободный текст | `support_chat_text()` | SUPPORT_CHAT |
| Callback `support:back` | `support_back_callback()` | VIEW_PROGRAM (с inject support history) |

### Fallbacks (из любого состояния)

| Триггер | Обработчик | Следующее состояние |
|---------|-----------|-------------------|
| `/support` | `support_start_callback()` | SUPPORT_CHAT |
| `/cancel` | `cancel()` | ConversationHandler.END |
| `/start` | `start_command()` (allow_reentry) | Зависит от состояния пользователя |

---

## Agent chat flow (VIEW_PROGRAM)

Полный цикл обработки текстового сообщения в `view_program_text()`:

```
1. Проверка pending_task_id (долгая генерация рекомендаций)
   -> если есть: проверить статус, показать результат или ждать

2. Сборка контекста:
   - profile_info: теги, keywords, nl_summary, company, business_objectives
   - recs_summary: rank, title, score, tags[:3], room, summary[:150] для каждого проекта
   - role_code: guest или business
   - support_chat_history: если вернулся из support (опционально)

3. Построение system prompt:
   build_agent_system_prompt(is_business, profile_info, recs_summary, num_recommendations)
   -> inject support_chat_history если есть

4. Chat history:
   program_chat.append({"role": "user", "content": message})
   if len > 20: trim from start

5. Celery task:
   agent_chat_task.delay(system_prompt, chat_history, AGENT_TOOLS)

6. Wait for result (timeout=15s, poll_interval=0.5s)

7. Dispatch:
   - type == "tool_call" -> диспетчеризация по tool_name
   - type == "text" -> отправка content пользователю
   - timeout / None -> "Обработка занимает больше времени..."

8. Chat history update:
   program_chat.append({"role": "assistant", "content": reply})

9. Support messages log:
   Сохранение user message + bot reply в support_messages (DB)
```

---

## System prompt (agent mode)

Строится в `build_agent_system_prompt()`:

```
Ты - AI-куратор Demo Day. Пользователь получил персональную программу проектов.
Отвечай кратко, по делу, на русском. Без эмодзи.

РОЛЬ ПОЛЬЗОВАТЕЛЯ: {гость | бизнес-партнер}

ИНСТРУМЕНТЫ (tools):
- show_project - показать детали ОДНОГО проекта по номеру
- show_profile - показать профиль пользователя
- compare_projects - сравнить 2-5 проектов
- generate_questions - подготовить вопросы для Q&A к проекту
- {get_followup | get_pipeline} - роль-зависимый инструмент
- rebuild_profile - перезапустить профилирование

ПРАВИЛА:
- Для сравнения ВСЕГДА вызывай compare_projects, НЕ пиши текстом
- show_project - ТОЛЬКО для одного проекта
- Для простых вопросов отвечай текстом из РЕКОМЕНДАЦИЙ
- Помогай планировать маршрут по залам

ПРОФИЛЬ:
{profile_info}

РЕКОМЕНДАЦИИ ({N} проектов):
{recs_summary}
```

---

## Profiling agent

LLM-диалог для сбора профиля. Промпт: `get_profile_agent_system()` в `profiling.py`.

### Параметры

- Максимум 2 LLM-ответа (ограничение в промпте: "после 2-го ОБЯЗАТЕЛЬНО action=profile")
- Минимум 1 уточняющий вопрос (промпт: "первое сообщение ВСЕГДА action=reply")
- Guard в коде: если 1 user message + 0 assistant -> принудительный follow-up
- json_mode=True, формат: `{"action": "reply"|"profile", ...}`

### Few-shot примеры

3 примера в промпте:
1. Студент с тегами NLP+Agents -> уточнение применения -> profile
2. Бизнес-партнер без тегов -> уточнение компании+задачи -> profile (с company, position, partner_status, business_objectives)
3. Абитуриент с 4 тегами -> уточнение фокуса -> profile

### Роль-зависимые контексты

| Роль | Стиль | Стратегия |
|------|-------|-----------|
| guest + student | Неформальный, на "ты" | Уточнить применение -> profile |
| guest + applicant | Мотивирующий, на "ты" | Объяснить теги простым языком + info ИТМО |
| guest + other | Профессиональный, на "вы" | Адаптировать под роль |
| business | Деловой, на "вы" | Компания + должность + цель -> profile |

### JSON output

```json
// Продолжение диалога
{"action": "reply", "message": "..."}

// Фиксация профиля (guest)
{"action": "profile", "interests": ["NLP", "Agents"], "goals": ["..."], "summary": "..."}

// Фиксация профиля (business)
{"action": "profile", "interests": [...], "goals": [...], "summary": "...",
 "company": "НЛМК", "position": "CTO", "partner_status": "potential",
 "business_objectives": ["technology"]}
```

---

## Stop conditions

| Условие | Поведение |
|---------|----------|
| `/start` из любого состояния | allow_reentry=True, restart flow |
| `/cancel` | ConversationHandler.END |
| Celery task timeout | Сообщение пользователю, предложение повторить |
| Нет мероприятия | "Сейчас нет запланированных мероприятий" -> END |
| Ошибка DB | "Ошибка. Попробуйте /start заново" -> END |

---

## Retry и fallback (LLM Client)

Реализовано в `llm_client.py`. Применяется ко всем LLM-вызовам.

### Retry policy

- MAX_RETRIES = 3
- Exponential backoff: `wait = 2^(attempt + 1)` секунд (2s, 4s, 8s при attempt=0,1,2)
- Key rotation: каждый retry может использовать другой API key

### Key rotation

```python
class KeyManager:
    # Round-robin по доступным ключам
    # KEY_COOLDOWN_SECONDS = 60 (пауза после ошибки)
    # Если все ключи в cooldown -> используется oldest failed
```

Ключи маркируются как failed при HTTP 401, 403, 429, 503. После 60 секунд ключ снова доступен.

### Fallback model

```python
FALLBACK_MODEL = "openai/gpt-4o-mini"
```

Переключение на fallback при:
- attempt == 1 (второй retry)
- Текущая модель != FALLBACK_MODEL
- HTTP status 400, 403, 404

### HTTP timeouts

```python
TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=180.0,   # 3 минуты на чтение
    write=30.0,
    pool=10.0,
)
```

### Цепочка деградации

```
Attempt 0: primary model (gpt-5.1) + key A
    -> fail (HTTP 429)
Attempt 1: primary model + key B
    -> fail (HTTP 400) -> switch to fallback model
Attempt 2: fallback model (gpt-4o-mini) + key C
    -> fail
RuntimeError("LLM failed after 3 attempts")
```

На уровне profiling_service:
```
RuntimeError -> graceful degradation:
  - chat_for_profile: "Расскажите, какие технологии вам интересны?" (без LLM)
  - extract_interests: {"tags": [], "keywords": []}
  - generate_llm_summaries: {pid: None for all}
  - generate_recommendations: tag overlap fallback
```

---

## LLM quality control

### JSON mode

- `send_chat_completion()` с `json_mode=True` -> `response_format: {"type": "json_object"}`
- Ответ парсится через `json.loads(content)`
- При `JSONDecodeError` -> retry

### Tool calling

- `send_chat_with_tools()` с явной схемой инструментов
- `tool_choice: "auto"` - LLM решает сам, нужен ли инструмент
- Парсинг `tool_calls[0].function.arguments` через `json.loads()`
- Только первый tool_call обрабатывается (single tool per turn)

### System prompt constraints

- Явные инструкции по формату ответа
- Запрет эмодзи
- Ограничение длины (2-3 предложения в профилировании)
- Правила выбора инструментов (compare_projects vs текст)
- Роль-зависимые стили общения

### Profiling guards

- Код принудительно отклоняет profile на первом сообщении (guard в `_onb_agent_turn`)
- Максимум 2 LLM-ответа зашито в промпте
- Валидация interests: только из списка допустимых тегов (в `extract_interests_from_text`)
