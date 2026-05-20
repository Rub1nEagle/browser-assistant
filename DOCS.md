# Browser Assistant — полная документация

> Автономный AI-агент, который управляет настоящим Chromium через Playwright и
> рассуждает LLM-моделью (Claude или GPT). На вход даётся свободная
> формулировка задачи — на выход агент сам открывает сайты, заполняет формы,
> читает страницы и отчитывается. Никаких заранее прописанных селекторов или
> сценариев.

---

## Содержание

1. [Зачем это](#1-зачем-это)
2. [Архитектура и поток данных](#2-архитектура-и-поток-данных)
3. [Установка и запуск](#3-установка-и-запуск)
4. [Конфигурация](#4-конфигурация)
5. [Способы использования](#5-способы-использования)
6. [Инструменты агента](#6-инструменты-агента)
7. [Внутренности: цикл ReAct, компрессия, рефлексия](#7-внутренности-цикл-react-компрессия-рефлексия)
8. [События и WebSocket-протокол Web UI](#8-события-и-websocket-протокол-web-ui)
9. [Безопасность и защита от деструктива](#9-безопасность-и-защита-от-деструктива)
10. [LLM-провайдеры и стоимость](#10-llm-провайдеры-и-стоимость)
11. [Логины и профиль браузера](#11-логины-и-профиль-браузера)
12. [Расширение: новые инструменты, провайдеры, UI](#12-расширение-новые-инструменты-провайдеры-ui)
13. [Тестирование](#13-тестирование)
14. [Поиск и устранение проблем](#14-поиск-и-устранение-проблем)
15. [Структура репозитория](#15-структура-репозитория)
16. [Глоссарий](#16-глоссарий)

---

## 1. Зачем это

Большинство «AI-агентов для браузера» либо записывают сценарии под конкретный
сайт (хрупко, ломается при первом редизайне), либо смотрят на пиксели
скриншота (дорого, медленно, ненадёжно на нестандартных контролах).

Browser Assistant работает иначе:

- **Видит структуру.** Каждый шаг агент получает дерево accessibility-снимка
  страницы — то же, что использует screen-reader. Кнопки, ссылки и поля
  размечены идентификаторами вида `[ref=e6]`.
- **Действует через те же идентификаторы.** `click(element_id="e6")`,
  `type(element_id="e9", text="…")`. Playwright сам резолвит ref в реальный
  DOM-элемент.
- **Рассуждает в цикле ReAct.** observe → think → act → tool_result → repeat.
- **Не привязан к сайту.** Любая страница, на которой есть accessibility-tree
  (то есть почти любая), — потенциально подъёмная задача.

Это делает агента устойчивым к редизайнам и пригодным для открытых задач:
«просмотри почту и удали спам», «найди три вакансии на hh.ru и пришли
ссылки», «открой статью, перескажи».

---

## 2. Архитектура и поток данных

```
            ┌───────────────────────────────────────────────────────────┐
            │                      Frontend                              │
            │   Web UI (static/index.html + app.js)   │   CLI (rich)     │
            └─────────────┬──────────────────────────┬──────────────────┘
                          │ WebSocket                │ stdin/stdout
                          ▼                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │       src/web/server.py   ←→   src/cli/app.py            │
            │  FastAPI + WS, AppState  │   Click commands              │
            └─────────────┬────────────┴───────┬──────────────────────┘
                          │      EventBus      │
                          │ (pub/sub события)  │
                          ▼                    ▼
            ┌──────────────────────────────────────────────────────────┐
            │                     agent.core.Agent                     │
            │   ReAct loop · Repeated-action detector · Reflection     │
            └──┬─────────────────┬──────────────┬─────────────────┬───┘
               │                 │              │                 │
               ▼                 ▼              ▼                 ▼
        LLMClient        ContextManager   ToolRegistry      IOChannel
        (Anthropic /     history +        15 инструментов   ask_user
         OpenAI)         scratchpad +     с Pydantic-       (CLI ←→ stdin,
                         observe          схемами           Web ←→ WS)
                         compression                 ▼
                                                ToolContext
                                                    │
                                                    ▼
                                           BrowserController
                                                    │
                                                    ▼
                                            Playwright
                                                    │
                                                    ▼
                                             Chromium (Xvfb)
                                                    │
                                                    ▼
                                          x11vnc → noVNC → iframe в UI
```

Ключевые наблюдения:

- **Frontend нейтрален к транспорту.** CLI и Web UI слушают один и тот же
  `EventBus`. Логика агента не знает, кто его подписчик.
- **`IOChannel` абстрагирует `ask_user`.** В CLI это блокирующий ввод через
  stdin в отдельном потоке. В Web — `Future`, которое резолвится
  WebSocket-сообщением `answer`.
- **Браузер «отделён» от UI.** Сам Chromium запущен внутри Xvfb-сессии и
  виден через noVNC. Web UI просто вставляет noVNC в iframe — никакой
  специальной IPC между UI и браузером не нужно.

---

## 3. Установка и запуск

### Вариант A — Docker (рекомендуется)

Подходит всем, в том числе Windows-пользователям. Браузер виден внутри
вкладки, профиль переживает рестарты.

```bash
git clone <repo> browser-assistant
cd browser-assistant

cp .env.example .env
# В .env: выбрать LLM_PROVIDER и заполнить ANTHROPIC_API_KEY или OPENAI_API_KEY.

docker compose up -d --build
# Первый билд: ~3-5 мин (Playwright-образ большой).
```

После запуска два URL:

- **Web UI** — http://localhost:8000  (основной интерфейс)
- **Сырой noVNC** — http://localhost:6080  (если хочется смотреть на браузер
  без UI)

Авторизация в сайтах — один раз через CLI внутри контейнера:

```bash
docker compose exec agent python -m agent login https://mail.yandex.ru
# Откройте http://localhost:6080, залогиньтесь руками в браузере,
# вернитесь в терминал и нажмите Enter — куки уже в профиле.
```

Остановить:

```bash
docker compose down       # сохранит профиль (volume `browser-profile`)
docker compose down -v    # снесёт профиль и логины — придётся логиниться заново
```

> **Apple Silicon.** Если на M-чипах Chromium падает на старте, в
> `docker-compose.yml` раскомментируйте `platform: linux/amd64` — будет
> работать через Rosetta, медленнее, но стабильно.

### Вариант B — локальный venv

Подходит для разработки. Откроется обычное окно Chromium на вашем рабочем
столе.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
.venv/bin/playwright install chromium

cp .env.example .env
# Заполните .env

.venv/bin/python -m agent run "открой https://example.com и расскажи, что на странице"
```

Профиль браузера лежит в `./.browser-profile/`. Это полноценный
persistent context Chromium — куки и localStorage переживают рестарты.

---

## 4. Конфигурация

Все настройки читаются из `.env` (через `python-dotenv`).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` или `openai` (последнее — также OpenRouter / DeepSeek / Ollama / любой OpenAI-совместимый). |
| `LLM_MODEL` | по провайдеру | Например, `claude-sonnet-4-6`, `gpt-4o`, `openai/gpt-4o-mini`. |
| `ANTHROPIC_API_KEY` | — | Обязателен при `LLM_PROVIDER=anthropic`. |
| `ANTHROPIC_BASE_URL` | — | Опционально, для прокси/реселлеров. |
| `OPENAI_API_KEY` | — | Обязателен при `LLM_PROVIDER=openai`. |
| `OPENAI_BASE_URL` | — | Эндпоинт. Примеры: `https://openrouter.ai/api/v1`, `https://api.deepseek.com`, `http://localhost:11434/v1`. |
| `MAX_STEPS` | `60` | Жёсткий потолок числа итераций ReAct. По достижении — `TaskFailed`. |
| `MAX_COST_USD` | `2.0` | Жёсткий бюджет на задачу. `0` — отключить. |
| `CONFIRM_DESTRUCTIVE` | `true` | Если включено, агент остановится и спросит перед потенциально разрушительным действием. См. §9. |
| `BROWSER_PROFILE_DIR` | `./.browser-profile` (локально) / `/data/profile` (Docker) | Путь к profile dir Chromium. |

`.env.example` — рабочий шаблон.

---

## 5. Способы использования

### 5.1. Web UI

`http://localhost:8000`. Слева — поле ввода, лента событий и блок «вопрос
агента»; справа — окно браузера (noVNC) и блок «память агента» (scratchpad).

Особенности интерфейса:

- **Примеры задач** — пресеты на пустом экране: нажатие подставляет
  формулировку в поле.
- **Шаги** — каждый шаг ReAct сворачивается в карточку с краткой сводкой
  («что сделано»). По клику разворачивается с tool-calls и метриками.
- **Стоимость** — счётчик и прогресс-бар по `MAX_COST_USD`. При 60% бар
  желтеет, при 90% — красный.
- **Тема** — кнопка ☀ / 🌙 в шапке. Сохраняется в `localStorage`.
- **Настройки** ⚙ — спрятать технические метрики, отключить «мысли»
  агента, автопрокрутку, добавить звуковое уведомление о завершении.
- **Вопросы агента** (`ask_user`) подсвечиваются жёлтой панелью внизу. Ответ
  — в текстовое поле, Enter отправляет.
- **Остановить** — кнопка `cancel`. Завершит задачу и эмитнет `TaskFailed`.

Hotkeys:
- `⌘/Ctrl` + `Enter` в textarea — запустить.
- `Esc` — закрыть модалку настроек.

### 5.2. CLI

```bash
python -m agent run "<задача>"
python -m agent login <url>
python -m agent serve --host 0.0.0.0 --port 8000
```

- `run` — запустить одну задачу. Stdout — лог событий в цвете (rich).
- `login` — открывает указанный URL в полноразмерном Chromium, ждёт ваш
  Enter; куки попадают в profile dir, далее агент уже залогинен.
- `serve` — поднять Web UI (FastAPI + uvicorn).

---

## 6. Инструменты агента

15 инструментов. Все определены в [src/agent/tools/registry.py](src/agent/tools/registry.py),
схемы аргументов — в [src/agent/tools/schemas.py](src/agent/tools/schemas.py).

### Чтение и навигация

| Tool | Аргументы | Что делает |
|---|---|---|
| `observe` | — | Снимает accessibility-снимок текущей страницы. Возвращает URL, title, YAML-дерево с `[ref=eN]` маркерами. **Ref'ы валидны только до следующего `observe`.** |
| `navigate` | `url` | Переходит на абсолютный URL. |
| `go_back` / `go_forward` | — | История браузера. Возвращает «no previous/forward page», если переходить некуда. |
| `scroll` | `direction` (down/up/top/bottom), `amount?` | Прокрутка. Для top/bottom — мгновенный скачок. |
| `wait_for` | `condition` (network_idle/load/url_contains/text_visible), `value?`, `timeout_seconds?` | Ожидает состояние страницы. Кидает ошибку по таймауту. |

### Взаимодействие

| Tool | Аргументы | Что делает |
|---|---|---|
| `click` | `element_id` | Кликает по ref из последнего `observe`. |
| `type` | `element_id`, `text`, `submit?` | Очищает поле и вводит текст. `submit=true` нажимает Enter. |
| `press_key` | `element_id`, `key` | Нажатие клавиши с фокусом на элементе. Синтаксис Playwright (`Enter`, `Escape`, `Control+A`). |
| `select` | `element_id`, `value` | Выбор в нативном `<select>`. Для кастомных дропдаунов из `<div>` — использовать `click`. |

### Извлечение и память

| Tool | Аргументы | Что делает |
|---|---|---|
| `extract` | `instruction` | Под-агент: отдельный LLM-вызов получает полное a11y-дерево и инструкцию вида «извлеки список последних 10 писем: subject, sender, snippet, is_unread». Дешевле и точнее, чем парсить snapshot в основном цикле. |
| `remember` | `key`, `value` | Сохраняет заметку в scratchpad. Виден агенту в системном промпте под `# Scratchpad`. **Переживает компрессию истории.** |
| `recall` | `key` | Читает запись из scratchpad. |

### Управление и завершение

| Tool | Аргументы | Что делает |
|---|---|---|
| `ask_user` | `question` | Останавливает агента и ждёт ответа пользователя (CLI: stdin; Web: жёлтая панель). |
| `done` | `report` | Финальный отчёт. **Завершает задачу** — больше шагов не будет. |

### Авто-нотации в результатах

Tool-результаты иногда обогащаются автоматическими маркерами:

- `[reflection] Page looks unchanged after this action…` — добавляется после
  мутирующего действия (`click`, `type`, …), если URL, title и длина body
  не изменились. Сигнал «действие не сработало, попробуй иначе».
- `[repeated-action detector] You have made the same turn (X) with identical arguments 3 times in a row…`
  — добавляется, когда три ассистент-хода подряд содержат одинаковый набор
  `(tool, args)`. Сигнал «ты застрял, поменяй подход».

Подробнее — см. [src/agent/browser/telemetry.py](src/agent/browser/telemetry.py)
и [src/agent/core.py](src/agent/core.py).

---

## 7. Внутренности: цикл ReAct, компрессия, рефлексия

### 7.1. ReAct loop

[src/agent/core.py:84-185](src/agent/core.py#L84-L185).

```
for step in 1..MAX_STEPS:
    response = llm.step(system, messages, tools)
    if response.text: emit AgentThinking
    context.record_assistant(response.blocks)
    if no tool_calls: emit TaskCompleted(response.text); return
    for call in tool_calls:
        emit ToolCallStarted
        if call is mutating: pre = take_snapshot()
        result = registry.dispatch(call)
        if call is mutating and ok: result += format_reflection(pre, post)
        emit ToolCallCompleted
    context.record_tool_results(result_blocks)
    emit ScratchpadUpdated
    if any result.is_terminal: emit TaskCompleted; return
    if cost >= MAX_COST_USD: emit TaskFailed; return
emit TaskFailed("step cap reached")
```

### 7.2. Сжатие истории `observe`

[src/agent/context/manager.py](src/agent/context/manager.py).

Полный a11y-снимок — десятки тысяч токенов. Если в истории несколько
последовательных `observe`, держать их все полные — дорого и бессмысленно:
страница уже обновилась.

Поэтому на каждый шаг `ContextManager.build_messages()` оставляет в полном
виде только **последний** результат `observe`, а старые подменяет на
одну строку:

```
[earlier observe — collapsed to save tokens] URL was https://…;
N interactive refs at the time. Refs from this observe are stale;
call observe() again to re-fetch.
```

Это сделано в build-фазе — оригинальные сообщения в `_messages` не
портятся. Если завтра захочется альтернативной политики (например,
оставлять последние 3) — поменять одну функцию.

### 7.3. Scratchpad

Простой `dict[str, str]`. Содержимое:
- Записывается через `remember(key, value)`.
- Дописывается к системному промпту под `# Scratchpad` каждый шаг.
- Не сжимается, не теряется.
- Показывается в правой панели Web UI.

Для долгих задач (например, «обработай 10 писем поочерёдно») это
основной носитель прогресса.

### 7.4. Рефлексия после мутации

[src/agent/browser/telemetry.py](src/agent/browser/telemetry.py).
Перед мутирующим вызовом снимается «отпечаток»: URL, title, длина body.
После вызова — снова. Если все три совпали — добавляется системная
заметка `[reflection] Page looks unchanged…`. Эта заметка попадает
**только** в tool_result, а не в системный промпт — агент видит её
ровно тогда, когда она актуальна.

### 7.5. Repeated-action detector

[src/agent/core.py:187-213](src/agent/core.py#L187-L213).
`deque` из 3 последних «турнов» (где турн = кортеж сигнатур всех tool_calls
в одном ассистент-сообщении). Если все три турна идентичны — добавляем
заметку «ты застрял, либо переосмотрись, либо `ask_user`, либо `done`» и
сбрасываем deque (чтобы не нудить на ту же серию повторно).

Важный нюанс: ловим повторы **на уровне турнов**, не отдельных вызовов.
Так модель, которая легитимно делает `remember + observe` дважды в одном
ходе, не триггерит детектор ложно.

---

## 8. События и WebSocket-протокол Web UI

### События (`src/agent/events.py`)

| Событие | Поля | Когда |
|---|---|---|
| `AgentStarted` | `task` | В самом начале `Agent.run`. |
| `LLMRequestStarted` | `step` | Перед каждым LLM-запросом. |
| `LLMRequestCompleted` | `step`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd?` | После ответа LLM. `cost_usd=None` — модель не в нашей таблице цен. |
| `AgentThinking` | `text` | Если ассистент вернул текстовый блок наряду с tool-calls. |
| `ToolCallStarted` | `tool`, `args` | Перед каждым tool_call. |
| `ToolCallCompleted` | `tool`, `args`, `result_summary`, `is_error` | После каждого tool_call. `result_summary` — однострочное сжатие до 160 символов. |
| `ScratchpadUpdated` | `entries: dict[str, str]` | После tool_results, если поменялся scratchpad (отправляется всегда — UI игнорирует пустые дельты). |
| `NeedsUserInput` | `question` | Когда сработал `ask_user` или гард деструктивного действия. |
| `TaskCompleted` | `report` | При `done(...)` или когда ассистент закончил без tool_call. |
| `TaskFailed` | `reason` | При исключении, превышении бюджета, кэпа шагов или отмене пользователем. |

### WebSocket-сообщения (Web UI ↔ сервер)

**Клиент → сервер:**

```json
{ "type": "run",    "task": "…" }       // запустить
{ "type": "cancel"               }       // отменить текущий
{ "type": "answer", "answer": "…" }     // ответ на ask_user
{ "type": "ping"                 }       // health
```

**Сервер → клиент:**

Любое событие из `agent.events`, сериализованное в `{"type": "ClassName", …поля}`.
Дополнительно для `LLMRequestCompleted` сервер добавляет `cumulative_cost_usd`
и `cost_partial` (чтобы клиент мог показать прогресс-бар).

Особые ответы:
```json
{ "type": "Error", "reason": "…" }       // невалидный input или ошибка start_run
{ "type": "pong" }                       // ответ на ping
```

### Жизненный цикл

- Сервер хранит `AppState` с **одной** активной задачей. Параллельный
  `run` отбивается через `_run_lock` + `is_running()`.
- WS-сессий может быть несколько, очереди подписаны на тот же `EventBus` —
  открытие второй вкладки покажет ту же ленту.
- При закрытии вкладки (`WebSocketDisconnect`) очередь снимается, задача
  продолжает идти.
- `cancel_run` отменяет `asyncio.Task`, дожидается выхода через `finally`
  (там `controller.stop()`), эмитит `TaskFailed(reason="run cancelled by user")`.

---

## 9. Безопасность и защита от деструктива

[src/agent/tools/policy.py](src/agent/tools/policy.py) + гард в
[src/agent/tools/registry.py:108-144](src/agent/tools/registry.py#L108-L144).

Перед `click`, `type(submit=true)`, `press_key`, `select` агент проверяет:

1. Метку элемента (`<role> "<name>"` из последнего `observe`).
2. Для `type` — введённый текст.
3. Для `select` — выбираемое value.

Если в этих строках встретилась подстрока вроде `delete`, `send`, `pay`,
`удал`, `отправ`, `оплат` (полный список — в коде) — агент **не делает**
действие, а вместо этого зовёт `ask_user`. Возможные ответы:

- `yes` / `да` / `ok` — разрешить разово.
- `always` / `всегда` / `все` — разрешить такой паттерн до конца сессии.
- Что-либо ещё — отменить. Агент получит `ToolResult(is_error=true)` и
  должен либо переключиться на другой элемент, либо завершить.

Отключается через `CONFIRM_DESTRUCTIVE=false` — только для доверенных
сред (CI, локальные эксперименты).

Другие рамки:
- **Нет произвольного JS.** Никаких `page.evaluate()` для агента. Всё —
  через инструменты.
- **Нет произвольных селекторов.** Только refs из последнего `observe`.
  Это и UX-выбор (модель меньше ошибается), и предохранитель.
- **Пинг сервиса.** В Docker compose порты 6080/8000 биндятся только на
  `127.0.0.1` — без явной аутентификации публиковать наружу не стоит.

---

## 10. LLM-провайдеры и стоимость

### Поддерживаемые

| Провайдер | `LLM_PROVIDER` | Адаптер | Модели по умолчанию | prompt caching |
|---|---|---|---|---|
| Anthropic | `anthropic` | [anthropic_client.py](src/agent/llm/anthropic_client.py) | `claude-sonnet-4-6` | да |
| OpenAI-совместимые | `openai` | [openai_client.py](src/agent/llm/openai_client.py) | `gpt-4o-mini` | в зависимости от backend |

Через `OPENAI_BASE_URL` адаптер `openai` работает с OpenRouter, DeepSeek,
локальным Ollama и любым другим OpenAI-совместимым эндпоинтом.

### Cost meter

После каждого LLM-вызова `LLMClient.estimate_cost_usd(...)` считает
стоимость по встроенной таблице. Если модель не известна — возвращает
`None`. UI и `AgentRun` отслеживают флаг «частично оценено» и показывают
`+?` рядом с суммой.

`MAX_COST_USD` — мягкое прерывание после очередного шага: задача
завершается с `TaskFailed(reason="cost cap of $X reached …")`. Это
защита от убегающих петель.

### Рекомендации

- Для сложных задач (Yandex.Почта, hh.ru, длинные SPA) — `claude-sonnet-4-6`
  или `gpt-4o`. Дешёвые модели типа `gpt-4o-mini` плохо планируют на
  потоках длиннее 5 шагов.
- Если бюджет ограничен — `gpt-4o-mini` + явные подсказки в задаче.
- Для приватности — Ollama + Llama-3.1-70B (через OpenAI-адаптер).

---

## 11. Логины и профиль браузера

Профиль Chromium хранится в одной папке (locally `./.browser-profile/`,
в Docker — на named volume `browser-profile:/data/profile`). Это полноценный
persistent context: куки, localStorage, IndexedDB и прочее.

### Залогиниться один раз

```bash
# Docker
docker compose exec agent python -m agent login https://mail.yandex.ru

# Локально
python -m agent login https://github.com
```

`login` открывает Chromium, ждёт, пока вы введёте логин/пароль/2FA руками
в noVNC, потом Enter в терминале — куки уже на диске. После этого все
`run` будут идти от вашего аккаунта.

### Сменить или сбросить

- Сменить — `docker compose exec agent python -m agent login <другой-url>`.
  Старые куки останутся, новые добавятся.
- Сбросить всё — `docker compose down -v` (снесёт volume профиля) или
  `rm -rf .browser-profile/` локально.

---

## 12. Расширение: новые инструменты, провайдеры, UI

### Добавить инструмент

1. Описать pydantic-модель аргументов в [src/agent/tools/schemas.py](src/agent/tools/schemas.py).
2. Написать async-обработчик `(ctx, **args) -> ToolResult` в
   [src/agent/tools/registry.py](src/agent/tools/registry.py).
3. Зарегистрировать в списке `_TOOLS` рядом с описанием для LLM.

Контракт обработчика:
- Бросать `BrowserError` — обернётся в `is_error=True` с понятным
  сообщением.
- Возвращать `ToolResult(content=…)` — попадёт в историю как tool_result.
- `is_terminal=True` + `final_report=…` — завершит run (как `done`).

### Добавить LLM-провайдера

1. Реализовать `LLMClient` ([src/agent/llm/base.py](src/agent/llm/base.py)).
   Метод `step(system, messages, tools)` принимает обобщённые `Message`/`Tool`
   и возвращает `ResponseEnvelope` с `blocks`, `usage`, `tool_calls`, `text`.
2. Добавить ветку в `build_agent()` ([src/agent/core.py:216-260](src/agent/core.py#L216-L260)).
3. По возможности добавить запись в `_DEFAULT_MODELS` ([src/agent/config.py:10-13](src/agent/config.py#L10-L13)).

### Расширить UI

- События уже сериализованы: `_serialize(event)` в `src/web/server.py`
  отдаёт dataclass как `dict`. Новые поля попадут в JSON без дополнительной
  настройки.
- Лента — `handleEvent(msg)` в [src/web/static/app.js](src/web/static/app.js).
  Добавьте `case "NewEvent":` и нужный рендер.
- Стили — переменные темы в `:root` и `[data-theme="light"]` в
  [src/web/static/style.css](src/web/static/style.css).

---

## 13. Тестирование

```bash
# Docker
docker compose exec agent pytest -q

# Локально
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Покрыто:

- Компрессия истории (`tests/test_context_manager.py`).
- Конвертация Block/Message между внутренним форматом и OpenAI/Anthropic.
- Эвристика рефлексии (`tests/test_telemetry.py`).
- Валидация tool-args через Pydantic.
- Политика деструктива.
- Оценка стоимости для известных/неизвестных моделей.

Тесты не делают живых LLM-вызовов и не запускают Chromium — должны
проходить за <1с.

Кроме того, есть `scripts/smoke_browser.py` — no-LLM smoke-тест на
работу браузерной части (полезно проверить, что Playwright/X11/noVNC
поднялись).

---

## 14. Поиск и устранение проблем

### Web UI открывается, но noVNC чёрный

- Проверьте, что контейнер запущен: `docker compose ps`.
- Откройте http://localhost:6080 в новой вкладке — есть ли там вообще
  noVNC.
- Если нет — посмотрите логи Xvfb/x11vnc/supervisord:
  `docker compose logs agent | tail -100`.
- В UI есть кнопка «перезагрузить просмотр» (↻) в шапке noVNC-блока.

### «ANTHROPIC_API_KEY is not set»

- Файл `.env` есть в корне проекта (рядом с `docker-compose.yml`).
- Перезапустите контейнер: `docker compose up -d --force-recreate`.

### Chromium падает при `controller.start()`

- Профиль был залочен предыдущим запуском. `docker compose down`, потом
  `docker compose up -d`.
- На Apple Silicon — раскомментируйте `platform: linux/amd64` в
  docker-compose.

### Задача висит без действия

- Открыта ли в UI жёлтая панель «Агент задаёт вопрос»? Если да —
  ответьте.
- Если нет — нажмите «остановить», проверьте лог в ленте (последний
  `tool-call` и его результат) и перезапустите задачу с уточнениями.

### Сумма стоимости с `+?`

Модель не в нашей таблице цен. См. `LLMClient.estimate_cost_usd()`
в адаптерах. Это **не значит «бесплатно»** — `MAX_COST_USD` не учтёт
такие шаги. Если работаете с экзотической моделью — допишите цены в
адаптер.

### Агент «застрял» в цикле одинаковых действий

- Должен сработать repeated-action detector через 3 одинаковых турна.
- Если нет — возможно, агент чередует два разных действия. Остановите,
  переформулируйте задачу с более явной целью, или добавьте подсказку
  про то, какой селектор/раздел использовать.

---

## 15. Структура репозитория

```
.
├── README.md                      — короткий quick-start
├── DOCS.md                        — этот документ
├── pyproject.toml                 — пакетная мета (loose deps)
├── requirements.txt               — pinned версии (lock для сборок)
├── requirements-dev.txt           — pytest, ruff
├── Dockerfile                     — Playwright base + Xvfb + x11vnc + noVNC
├── docker-compose.yml             — единственный сервис `agent`, profile-volume
├── .env.example                   — шаблон конфигурации
│
├── prompts/
│   ├── system.md                  — основной системный промпт агента
│   └── extractor.md               — промпт для sub-agent `extract`
│
├── src/agent/                     — ядро
│   ├── __main__.py                — entrypoint (delegates to cli.app)
│   ├── config.py                  — Settings.load() из .env
│   ├── events.py                  — EventBus + dataclass-события
│   ├── core.py                    — Agent.run, build_agent, ReAct loop
│   │
│   ├── llm/
│   │   ├── base.py                — LLMClient ABC, ResponseEnvelope, Usage
│   │   ├── types.py               — Block, Message, Tool
│   │   ├── anthropic_client.py    — Anthropic + prompt caching
│   │   └── openai_client.py       — OpenAI / OpenRouter / DeepSeek / Ollama
│   │
│   ├── browser/
│   │   ├── controller.py          — Playwright lifecycle, действия, _settle
│   │   ├── observe.py             — page.aria_snapshot(mode="ai") wrapper
│   │   └── telemetry.py           — pre/post-action snapshots, рефлексия
│   │
│   ├── tools/
│   │   ├── registry.py            — 15 инструментов + дестрктив-гард
│   │   ├── schemas.py             — Pydantic-модели аргументов
│   │   ├── policy.py              — детектор destructive substring + parse_confirmation
│   │   └── extract.py             — map-reduce sub-agent для длинных страниц
│   │
│   ├── context/
│   │   └── manager.py             — history + scratchpad + сжатие observe
│   │
│   └── io/
│       ├── channel.py             — IOChannel ABC + StdinChannel
│       └── web_channel.py         — WS-channel (future + emit NeedsUserInput)
│
├── src/cli/
│   ├── app.py                     — `python -m agent run|serve` (Click + Rich)
│   └── login.py                   — `python -m agent login <url>`
│
├── src/web/
│   ├── server.py                  — FastAPI + WS, AppState (one-task-at-a-time)
│   └── static/
│       ├── index.html             — UI
│       ├── style.css              — темы + компоненты
│       └── app.js                 — WS-клиент, рендер ленты
│
├── docker/
│   ├── entrypoint.sh              — стартует supervisord
│   └── supervisord.conf           — Xvfb + x11vnc + websockify(noVNC)
│
├── scripts/
│   └── smoke_browser.py           — проверка браузерного пути без LLM
│
└── tests/                         — pytest, без сети и без браузера
```

---

## 16. Глоссарий

- **ReAct loop** — паттерн «Reason + Act»: LLM получает контекст, выдаёт
  мысль + tool_call, получает tool_result, и так до завершения.
- **Accessibility snapshot (a11y-tree)** — структурное представление
  страницы для screen-reader'ов. Каждый интерактивный элемент имеет роль,
  имя и (в AI-mode Playwright) уникальный ref.
- **Ref** — короткий идентификатор вида `e6`, выданный последним `observe`.
  Резолвится в реальный элемент селектором `aria-ref=e6`. Инвалидируется
  следующим `observe`.
- **Scratchpad** — `dict[str, str]`, видимый агенту в системном промпте.
  Переживает компрессию истории.
- **Reflection** — авто-заметка в результате мутирующего действия, если
  страница не изменилась.
- **Repeated-action detector** — авто-заметка, когда модель делает три
  одинаковых турна подряд.
- **Tool result** — блок типа `tool_result`, который агент видит в
  следующем шаге как ответ на свой `tool_use`. У него есть `is_error`,
  и эту ошибку модель видит в-band.
- **Persistent context** — режим Playwright, в котором браузер использует
  заранее заданную папку с профилем. Куки, localStorage, IndexedDB
  переживают перезапуски.
- **noVNC** — HTML-клиент VNC. Внутри Docker-контейнера Xvfb отрисовывает
  Chromium в виртуальный display, x11vnc публикует его как VNC,
  websockify+noVNC оборачивает в HTTP — открывается в любой вкладке.
