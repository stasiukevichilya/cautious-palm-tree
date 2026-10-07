# Агент в браузере: Open WebUI

Open WebUI (v0.11.4) для локальной сети на порту **1098**. Что в него подключено:

- все LLM стека;
- скиллы Claude Code, pi и opencode из WSL;
- терминал агента (Open Terminal) с проектами из `~/mlstuff/projects`;
- генерация изображений: кнопка «Image» и MCP-инструменты;
- Blender на Windows через MCP (blender-mcp);
- сжатие контекста billion-context и Headroom (отдельные тумблеры в чате) и индикатор использования контекста.

Всё работает в контейнерах compose с профилем `agent`, на хост ничего не ставится, кроме аддона
в Blender на Windows.

| Сервис | Что делает | Порт |
|---|---|---|
| `agent-webui` | Open WebUI, данные в `outputs/agent/webui` | `0.0.0.0:1098` |
| `agent-terminal` | Open Terminal: shell и файлы агента (`outputs/agent/terminal` = `/home/user`), скиллы | только внутри сети `agent` |
| `images` | MCP `generate_image` и OpenAI `/v1/images/generations` поверх sdxl / qwen-image | `127.0.0.1:8088` |
| `images-uc` | генерация в Open WebUI: sdxl, qwen-image и qwen-image-uc, берётся первый запущенный; доступен только Open WebUI (сеть `agent-uc`), не боту | — |
| `bili` | billion-context 0.1.185, прокси сжатия контекста перед LLM, без автообновления | — |
| `headroom` | Headroom 0.40.0, прокси сжатия вывода инструментов / JSON / кода перед LLM, без телеметрии; Open WebUI, pi и opencode | `127.0.0.1:8091` |
| `blender-mcp` | MCP-сервер mcp-for-blender 2.1.8 → аддон в Blender на Windows (`host.docker.internal:9876`) | — |

GPU эти сервисы не занимают. Переключение моделей и `make models-stop` их не останавливают, кроме
`images` и `images-uc`: они следуют за генераторами (`make images-sync` в конце каждой цели модели и `make agent`).
`images` работает, пока запущен sdxl или qwen-image, `images-uc` — пока запущен любой из sdxl, qwen-image,
qwen-image-uc. Без генератора оба остановлены, MCP-инструменты Images / Images UC в Open WebUI недоступны.

## Запуск

```bash
make agent-key     # добавляет в .env AGENT_SECRET_KEY, AGENT_TERMINAL_KEY, AGENT_ADMIN_EMAIL/PASSWORD
make agent-build   # образы images, blender-mcp, bili, headroom + pull Open WebUI / Open Terminal
make agent         # http://<IP WSL>:1098, вход — AGENT_ADMIN_EMAIL / AGENT_ADMIN_PASSWORD из .env
make agent-functions  # фильтры Billion context, Headroom и Context usage
make qwen-mtp      # (или qwen / bonsai-mtp) — модель поднимается отдельно
```

Нужен `TGBOT_ADMIN_KEY` (`make tgbot-key`). Этот ключ закрывает API `images` так же, как
остальные сервисы стека.

Администратор создаётся при первом старте, регистрация закрыта. Других пользователей
добавляют в Admin → Users.

**Доступ из локальной сети.** Порт публикуется на `0.0.0.0` (`AGENT_BIND` в .env меняет адрес).
В WSL с mirrored-сетью нужно ещё разрешить входящие на 1098 в файрволе Windows
(PowerShell от администратора):

```powershell
New-NetFirewallRule -DisplayName "Open WebUI 1098" -Direction Inbound -Protocol TCP -LocalPort 1098 -Action Allow -Profile Private
```

## Проекты

`~/mlstuff/projects` смонтирован в терминал агента как `~/projects` (`/home/user/projects`) на
чтение и запись. Агент правит файлы на месте, UID совпадает с хостовым (1000), git работает
с конфигом репозитория. Другой каталог задаётся в .env: `AGENT_PROJECTS=/путь`, затем `make agent`.

Как работать:

1. **Один раз выберите терминал**: кнопка «+» / «Интеграции» у поля ввода → Terminal. Выбор хранится
   в браузере (`localStorage`) и действует во всех чатах этого браузера. Сервер задать его по
   умолчанию не может. Без выбранного терминала у модели нет доступа к файлам: она видит только
   инструменты MCP.
2. Справа появится файловая панель терминала, проекты лежат в `/home/user/projects`.
3. Попросите, например: «открой ~/projects/coloringus». `AGENTS.md` в рабочем каталоге
   Open WebUI подхватывает на каждом ходе.

Встроенный Code Interpreter (Pyodide) выключен. Это Python в браузере со своей виртуальной ФС
`/mnt/uploads`: модель уходила в него вместо терминала и не находила проекты.

**Docker хоста.** В терминал проброшен сокет `/var/run/docker.sock`, есть `docker` и `docker compose`.
GID сокета Makefile передаёт как `AGENT_DOCKER_GID`, поэтому запускайте через `make agent`. Проекты
дополнительно смонтированы по пути хоста (`~/mlstuff/projects`): демон ищет файлы bind-mount'ов
на хосте, поэтому `docker compose` нужно запускать из `~/mlstuff/projects/<проект>`, а не из
`~/projects/<проект>`. Модель об этом знает из `OPEN_TERMINAL_INFO`. Сокет даёт права root на хосте:
любой, кто может писать модели с выбранным терминалом в Open WebUI, может делать на хосте всё.

**Порты 7070–7079** опубликованы из терминала в локальную сеть (`AGENT_BIND`). Dev-сервер должен
слушать `0.0.0.0` на одном из них, например `python3 -m http.server 7070 --bind 0.0.0.0`
или `npm run dev -- --host 0.0.0.0 --port 7071`. Адрес: `http://<IP>:7070`. Для доступа с других
устройств нужно правило файрвола Windows, как для 1098.

## Модели

`OPENAI_API_BASE_URLS` перечисляет `qwen`, `qwen-mtp`, `bonsai-mtp` и `strata` (последним, `make strata`).
Встроенные инструменты Open WebUI без переключателя в чате (время, базы знаний, заметки, задачи,
автоматизации, календарь и др.) выключены для всех моделей (`DEFAULT_MODEL_METADATA`); модели можно
вернуть категорию в Workspace → Models → Builtin Tools. Фильтр `tool_args_guard` чинит в истории
аргументы вызова, оборванного лимитом контекста (иначе Strata отвечает 400 на каждое новое сообщение).

**Уровень рассуждений** выбирается кнопкой с лампочкой рядом с выбором модели (`agent/webui/loader.js`):
Авто / Выкл / Низкий / Средний / Высокий. Выбор один на все чаты и модели, хранится в браузере
(localStorage) и уходит в запрос чата как `params.reasoning_effort`; «Авто» ничего не добавляет, тогда
действует параметр модели (у Strata в Workspace → Models стоит `high`) или умолчание сервера. Фильтр
`reasoning_effort` переводит значение в `chat_template_kwargs` (`enable_thinking: false` / `reasoning_effort:
low|medium|xhigh`), как их читают шаблоны Qwen3.8 в llama.cpp (`--jinja`) и Strata; новые модели с таким
шаблоном работают без настройки, модели без этих переменных шаблона их просто не используют.
После правки `loader.js` / `custom.css` нужен рестарт `agent-webui` (файлы копируются при старте). В списке моделей есть
только запущенная. У остановленных Docker DNS не знает имени, ответа приходится ждать около 2 с
(`dns_opt` в compose; без него было 8 с).

Настройки из compose записываются в базу только при первом старте, дальше действует то, что
изменено в админке. Применить compose заново: `AGENT_RESET_CONFIG=true make agent`. При этом
сбрасываются настройки, сделанные через Admin Settings. Чаты, пользователи, модели и скиллы
Workspace остаются.

## Скиллы

При старте `agent-terminal` скрипт `agent/sync_skills.py` создаёт симлинки в `~/.cptr/skills`.
Источники монтируются только на чтение, в порядке приоритета (при совпадении имени выигрывает первый):

1. Claude Code: `~/.claude/skills/*` и синхронизированные `~/.claude/skills/synced/*/*`;
2. скиллы установленных плагинов Claude Code (`~/.claude/plugins/installed_plugins.json`);
3. `~/.agents/skills` (opencode, `npx skills`);
4. `~/.config/opencode/skills`;
5. pi: `~/.pi/skills`, `~/.pi/agent/skills` и скиллы пакетов из `~/.pi/agent/settings.json`.

После установки или удаления скиллов на хосте выполните `make agent-skills`: терминал
перезапустится и покажет, что подключено. Скилл без `description` пропускается.

Как пользоваться:

- в чате выберите терминал (иконка терминала у поля ввода);
- модель видит каталог скиллов и загружает нужный сама; `$имя` подключает скилл явно;
- скрипты из `scripts/` агент запускает в терминале: python3, node и git там есть.

Скиллы, которые агент создаёт сам (`/skills:create`), сохраняются в
`outputs/agent/terminal/.agents/skills` и имеют приоритет над хостовыми. Кроме того, скиллы можно
добавлять в Workspace → Skills: импорт `.md` или ручное создание, затем привязка к модели в
Workspace → Models.

## Изображения

- **Кнопка «Image» и встроенный инструмент `generate_image`.** Движок `openai` →
  `http://images-uc:8080/v1`, модель `auto`, то есть первый запущенный генератор из sdxl,
  qwen-image и qwen-image-uc. Работает и после `make uc-bonsai`. В Admin → Images вместо `auto`
  можно указать конкретный генератор.
- **MCP-инструменты.** **Images UC** работает с теми же тремя генераторами. **Images** — только
  sdxl / qwen-image, это тот же набор, что у бота. Модель сама пишет промпт и вызывает
  `generate_image`, картинка появляется в чате.
- **Изоляция.** sdxl и qwen-image подключены и к сети `agent-uc`, чтобы до них доходил `images-uc`.
  Бот в этой сети не состоит и не видит ни `images-uc`, ни `qwen-image-uc`.
- Генератор поднимается отдельно (`make sdxl`, `make qwen-image`, `make qwen-image-uc`).
  Помните, что LLM 27B и генератор вместе в VRAM не помещаются. Исключение — `make uc-bonsai`
  (bonsai-mtp + qwen-image-uc).
- MCP-серверы и терминал без явной выдачи прав доступны только администратору. Права другим
  пользователям выдаются в Admin → Settings → External Tools / Integrations. Images UC лучше
  оставить только администратору.

API `images` с хоста (Bearer `TGBOT_ADMIN_KEY`):

```bash
curl -s http://127.0.0.1:8088/v1/images/generations -H "Authorization: Bearer $TGBOT_ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"prompt": "a red teapot", "model": "auto"}'
```

Код генерации общий с Telegram-ботом: `services/tgbot/images.py` копируется в образ `images` при сборке
(additional context `tgbot`). Бот вызывает генерацию через тот же модуль без MCP, см.
[services/TGBOT-RU.md](../services/TGBOT-RU.md).

## Billion context, Headroom и использование контекста

[billion-context](https://www.npmjs.com/package/billion-context) — прокси между чатом и моделью.
Он сворачивает старую часть диалога в подробные сводки и даёт модели инструменты `compress`,
`decompress`, `search_context` и `acp_status`. Так длинный чат помещается в окно модели.

- **Включение в чате.** Тумблер **Billion context** у поля ввода («+» / «Интеграции»),
  по умолчанию выключен. Фильтр `agent/functions/billion_context.py` направляет запрос на копию модели
  `bili.<модель>` (подключения `http://bili:8787/bili/http://<модель>:8080/v1` с `prefix_id: bili`).
  Сессия сжатия привязана к чату через `prompt_cache_key = owui-<chat_id>`. Копии `bili.*` скрыты
  из выбора моделей.
- **Стоимость.** Прокси добавляет около 4 тыс. токенов: инструменты и инструкции сжатия. Для коротких
  чатов включать его незачем.
- **Без автообновления.** Версия закреплена в образе (`agent/bili/Dockerfile`, `BILI_VERSION`).
  Запуск: `bili --no-auto-update --host 0.0.0.0 --port 8787`. Дополнительно выключено:
  - `ACP_AUTO_UPDATE=0` и `"autoUpdate": false` в конфиге;
  - проверка critical advisories (`BILI_ADVISORY_CHECK=0` / `"advisoryCheck": false`), иначе
    они ставят другую версию даже при выключенном автообновлении;
  - сам пакет принадлежит root, а прокси работает от `node`, так что переписать себя он не может.

  Обновление вручную: поменять `BILI_VERSION`, затем `make agent-build agent`.
- **Окна моделей** заданы в `agent/bili/billion-context.json`. Если меняете `*_CTX_SIZE` в .env,
  поправьте и их. Для Strata там 100000 при реальных 131072: bili недооценивает её токены примерно
  на 25%, а запас нужен, чтобы большой вывод инструмента не вывел запрос за окно.
- **Состояние сессий и лог:** `outputs/agent/bili` (`state/billion-context/bili.log`).

**Headroom.** [Headroom](https://github.com/headroomlabs-ai/headroom) — другой прокси сжатия. Диалог
он не сворачивает. Он сжимает содержимое промпта до отправки в модель: вывод инструментов, логи,
JSON (SmartCrusher), код (по AST) и текст (локальная ONNX-модель Kompress). Оригиналы остаются у прокси
и доступны модели по запросу. Работает независимо от billion-context.

- **Включение в чате.** Отдельный тумблер **Headroom** (`agent/functions/headroom.py`) направляет запрос
  на `hr.<модель>`. Это подключения `http://headroom:8787/v1` с `prefix_id: hr`. Модель выбирает
  заголовок `x-headroom-base-url` из конфига подключения, поэтому один контейнер обслуживает все модели.
  Список моделей у этих подключений статический (`model_ids`): `/v1/models` Headroom заголовок не учитывает.
- **По умолчанию включён** в новых чатах (`defaultFilterIds` в `DEFAULT_MODEL_METADATA`), Billion context —
  выключен. Почему так — `agent/CTX-BENCH-RU.md`. Выключить можно в меню Integrations у поля ввода.
- **«Изображение» по умолчанию выключено:** `"defaultFeatureIds": []` в `DEFAULT_MODEL_METADATA`. Если у модели
  в Workspace → Models свои «Default Features», действуют они (у qwen3.8-27b-mtp — веб-поиск и интерпретатор кода).
- **Если Headroom не отвечает,** фильтр проверяет `/health` (кэш 15 с) и отправляет запрос мимо него со статусом
  «Headroom недоступен, запрос идёт без Headroom»; плашка Headroom под таким ответом не ставится. Без этой
  проверки чат падал бы с «Server Connection Error»: копии `hr.*` остаются в списке моделей и при
  остановленном контейнере.
- **Оба тумблера сразу:** `hb.<модель>`, цепочка headroom → billion-context → модель. Через billion-context
  нет живых `timings`, под ответом только число токенов.
- **pi и opencode** ходят в модели через Headroom: `http://127.0.0.1:8091/v1` и заголовок
  `x-headroom-base-url: http://<qwen|qwen-mtp|bonsai-mtp>:8080` у каждого провайдера
  (`~/.pi/agent/models.json`, `~/.config/opencode/opencode.jsonc`). Напрямую — порты 8080 / 8081 / 8089.
- **Окна моделей** заданы в `HEADROOM_MODEL_LIMITS`: незнакомым моделям Headroom иначе ставит 128K. Имена — как
  их шлют Open WebUI и pi / opencode.
- **Подключения из compose** попадают в базу только при первом старте Open WebUI. На уже
  настроенной базе нужен `AGENT_RESET_CONFIG=true make agent` (сбрасывает настройки Admin Settings)
  или ручное добавление в Admin → Settings → Connections.
- **Без телеметрии и автообновления.** Версия закреплена в `agent/headroom/Dockerfile`
  (`HEADROOM_VERSION`). Выключены `HEADROOM_BEACON`, `HEADROOM_UPDATE_CHECK`, `DO_NOT_TRACK=1` и
  `--no-telemetry`. Разрешены только upstream'ы из `HEADROOM_ALLOWED_BASE_URLS`. Порт опубликован только на
  `127.0.0.1:8091` (для pi / opencode), поэтому слушать без токена разрешено явно
  (`HEADROOM_ALLOW_UNAUTHENTICATED_BIND`).
- **Данные:** `outputs/agent/headroom`. Там же веса Kompress (~500 МБ, качаются с Hugging Face при первом запросе).

**Использование контекста — как в веб-интерфейсе llama.cpp.**

- **Кольцо слева от выбора модели** показывает долю занятого контекста: жёлтое от 70%, красное
  от 90%. По клику открывается панель:
  - «Контекст · занято / окно», полоска, «% занято» и «осталось»;
  - за весь чат: сколько токенов промпта обработано (без кэша llama.cpp) и сколько сгенерировано;
  - этот ход: промпт, сгенерировано, всего в KV-кэше;
  - средняя скорость.
- **Под сообщениями** — строка как в llama.cpp, с подсказками при наведении:
  - под вашим сообщением: токены, время и скорость обработки промпта;
  - под ответом: плашка модели (`qwen3.8 [27B] [mtp]`, плюс `billion-context` или `Headroom`, если
    был включён), сгенерированные токены, время генерации и t/s.
- **Вживую, пока идёт ответ.** Во время обработки промпта растёт счётчик токенов промпта, затем
  под ответом идут таймер, токены и t/s. После завершения строки заменяются итоговыми с сервера.
  Фильтр добавляет в запрос к llama.cpp `timings_per_token` и `return_progress` (valve
  `live_timings`). Open WebUI пересылает эти данные в браузер событиями `chat:completion` по
  WebSocket, а `loader.js` читает их, подменяя `WebSocket` до подключения фронтенда.

Как это устроено:

- Глобальный фильтр `agent/functions/context_usage.py` после каждого ответа сохраняет в сообщение
  `contextStats`: окно из `/props` llama.cpp (`n_ctx`) и данные из `usage` / `timings`.
- `agent/webui/loader.js` и `custom.css` рисуют интерфейс. Это штатные хуки Open WebUI
  (`/static/loader.js`, `/static/custom.css`): каталог монтируется в контейнер и копируется при
  старте. После правок нужен `docker compose restart agent-webui`.

Ограничения:

- Через billion-context llama.cpp `timings` не доходят. Тогда нет живого режима и видны только
  токены, без времени и скорости, а KV-кэш считается как prompt + completion. В ходе с вызовами инструментов это
  завышенная оценка, панель подписывает её «оценка (без timings)».
- Ответы до установки фильтра статистики не имеют.
- Строка «Контекст: X / N» над ответом выключена (valve `show_status` фильтра).

Фильтры и скрытие `bili.*` и `hr.*` ставит `make agent-functions`; запускайте его после правок в
`agent/functions/`.

## Blender (blender-mcp)

Аддон в Blender на Windows открывает сокет на `localhost:9876`. Контейнер `blender-mcp` подключается
к нему через `host.docker.internal`: Docker Desktop пробрасывает это имя на loopback Windows.
Пакет blender-mcp поддерживает только stdio, поэтому `agent/blender-mcp/run.py` запускает его как
streamable HTTP с Bearer-ключом (`TGBOT_ADMIN_KEY`).

Подготовка (один раз):

```bash
make agent-build      # образ local/ml-blender-mcp:1
make blender-addon    # кладёт blender_mcp.py в %APPDATA%\Blender Foundation\Blender\4.0\scripts\addons
                      # другая версия Blender: make blender-addon BLENDER_VERSION=4.2
```

В Blender:

1. Edit → Preferences → Add-ons, найти «MCP for Blender», включить.
2. 3D View → `N` → вкладка «MCP for Blender» → Connect / Start server (порт 9876).

В Open WebUI в чате включите инструмент **Blender**. Модель может:

- смотреть сцену (`get_scene_info`, `look` — скриншот вьюпорта);
- выполнять Python в Blender (`execute_blender_code`);
- искать и импортировать ассеты (`search_assets`, `import_asset`).

Сервер и аддон берутся из одного пакета, поэтому их версии совпадают. После обновления
`requirements.lock` выполните `make agent-build blender-addon` и перезапустите Blender.

- **Безопасность.** `execute_blender_code` — это произвольный Python на Windows с правами вашего
  пользователя. Подключение доступно только администратору Open WebUI, и выдавать его другим не стоит.
- **Телеметрия.** Выключена переменными окружения. Кроме того, согласие принудительно выставлено
  в «нет», потому что загрузку скриншотов и записи траекторий пакет иначе включает по флажку в аддоне.
- **Внешние сервисы.** Ассеты (Poly Haven, Sketchfab) и `generate_3d` (Hyper3D, Hunyuan3D) ходят
  в интернет из Blender и включаются в панели аддона.
- **Другой порт.** Если сменили порт в аддоне, укажите его в .env: `BLENDER_PORT=…`, затем `make agent`.

Проверка без UI: `make logs-agent`. При вызове инструмента без запущенного Blender в логе будет
`Connection refused` от `host.docker.internal:9876`.

## Обслуживание

```bash
make logs-agent    # логи всех сервисов агента
make agent-stop    # остановить
make images-test   # тесты images без сети
```

Метрики `images` и `images-uc` собирает Prometheus (`images_requests_total`,
`images_generation_seconds`).
