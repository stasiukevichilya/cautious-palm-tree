# План: DuckDuckGo MCP-сервер для локальных моделей

Цель: дать локальным моделям (Qwen3.8-27B / qwen-mtp / bonsai-mtp, llama.cpp)
веб-поиск через [duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server):
инструменты `mcp__ddg__search` и `mcp__ddg__fetch_content`.

## Текущее состояние (проверено по репо)

- Локальные модели: `qwen` :8080, `qwen-mtp` :8081, `bonsai-mtp` :8089 (llama.cpp,
  OpenAI-совместимый API). Тool-call формат у модели уже проверен `smoke-test.py`
  (выдача tool call + продолжение после результата).
- DSH (deepseek-harness, `@deepseek-ai/dsh`) — агентный harness, основной клиент
  локальных моделей. **Сейчас удалён из compose.yaml** (коммит `8f114d1`), но
  `dsh/Dockerfile` на месте, а README-RU.md (раздел 6) его описывает — документ
  частично устарел. Старый блок сервисов `dsh` + `dsh-forward` + volume `dsh-data`
  есть в `git show 8f114d1^:compose.yaml`.
- DSH нативно поддерживает MCP: запись `@deepseek-ai/dsh-mcp-client` (transports
  `stdio` / `streamable-http`), инструменты появляются как `mcp__<serverName>__<tool>`,
  auto-reconnect с backoff, таймаут на вызов `toolCallTimeoutMs` (default 60 c).
  Пользовательский конфиг: `$DSH_HOME/cordis.patch.yml` (volume `dsh-data`) или
  флаг `--patch <file>`.
- MCP-поддержки в проекте сейчас нигде нет (`rg -i mcp` по коду пусто).
- duckduckgo-mcp-server 0.7.0: Python ≥3.10, `uvx duckduckgo-mcp-server`;
  инструменты `search` и `fetch_content`; transports stdio (default), sse,
  streamable-http (путь `/mcp`, порт 8000, default bind 127.0.0.1).
  Env: `DDG_SAFE_SEARCH`, `DDG_REGION`, `DDG_RATE_LIMIT_STRATEGY`,
  `DDG_SEARCH_RPM` (default 30), `DDG_FETCH_RPM` (default 20), `DDG_CACHE_TTL`
  (default 300 s, in-memory кэш страниц), `DDG_PARSE_MODE` (text|main|markdown),
  `DDG_REF_URL_THRESHOLD`. Search-backend по умолчанию `auto`: httpx, при блокировке
  (пустой HTTP 202 / 403 от html.duckduckgo.com) — fallback на curl_cffi
  (Chrome TLS-имперсонация), для fallback нужен опциональный extra `[browser]`.
  SSRF-защита: `fetch_content` по умолчанию не ходит в private/loopback адреса
  (для нас это плюс — модель не заглянет во внутренние сервисы).

## Подход

**Отдельный сервис `ddg-mcp` (streamable-http) + подключение в DSH.**
Почему: стиль проекта (сервис = compose + Makefile + healthcheck + digest в .env);
независимо тестируется curl'ом (JSON-RPC); переиспользуемый для любых будущих
MCP-клиентов (в т.ч. tgbot); кэш страниц живёт между перезапусками DSH; образ DSH
не раздувается (uv/python внутрь него не нужны).

Рассмотренная альтернатива (слабее): stdio прямо в контейнере DSH
(`uvx duckduckgo-mcp-server`) — минус один контейнер, но скачивание пакета при
первом запуске, без кэша между restart и без standalone-теста.

## Шаги работы

### 1. Сервис `ddg-mcp` — `ddg/Dockerfile` + compose.yaml
- Образ: `python:3.13-slim` + `uv`; на build `duckduckgo-mcp-server[browser]`
  (curl_cffi — защита от TLS-фингерпринт-блокировки DDG; ~+20 МБ).
- Command:
  `duckduckgo-mcp-server --transport streamable-http --host 0.0.0.0 --port 8000 --allowed-hosts ddg-mcp`
  (`--allowed-hosts` вместо `--disable-dns-rebinding-protection`: DSH ходит с
  Host: ddg-mcp, без allow-list будет 421 Misdirected Request).
- Env: `DDG_SEARCH_RPM=30`, `DDG_FETCH_RPM=20` (defaults), опционально
  `DDG_PARSE_MODE`, `DDG_SAFE_SEARCH`, `DDG_REGION` (см. открытые вопросы).
- Порт хоста: `127.0.0.1:8088:8000` — 8088 свободен (занятые: 8080-8087, 8089,
  9835, 9090, 8889, 3055).
- Healthcheck: python urllib-проверка `/mcp` (статус, который вернёт GET без
  сессии, уточнить при реализации; худший случай — TCP-чек).
- `<<: *common`, без GPU, без volumes (кэш in-memory).
- Пин digest образа в `.env` (`DDG_IMAGE=`) + добавить в `prepare.sh`
  (конвенция проекта). Примечание: текущий `prepare.sh` уже ссылается на
  отсутствующий `docker-wsl.sh` — поправить при случае.

### 2. MCP-запись для DSH — `dsh/cordis.patch.yml` (в репо)
```yaml
- insert:
  - id: mcp-ddg
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: ddg
      transport: streamable-http
      url: http://ddg-mcp:8000/mcp
      toolCallTimeoutMs: 120000
```
- Монтить `./dsh/cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml:ro`
  (применяется ко всем профилям автоматически); запасной вариант — `--patch`
  в command сервиса.
- `toolCallTimeoutMs: 120000` — default 60 c может не хватить на `fetch_content`
  медленных страниц.
- Env-скраббинг DSH (убирает `*TOKEN*`/`DSH_*` у stdio-детей) нас не касается:
  streamable-http — обычный HTTP.

### 3. Восстановить сервис DSH в compose.yaml (см. вопрос 1)
- Вернуть блоки `dsh` + `dsh-forward` (socat 3081→3080) + volume `dsh-data`
  из `git show 8f114d1^:compose.yaml`, добавить монт `./dsh/cordis.patch.yml`
  и `depends_on: [ddg-mcp, otel]`.
- `DSH_VERSION` сейчас отсутствует в `.env` — восстановить пин (prepare.sh /
  ручная установка) и проверить, что закреплённая версия содержит
  `@deepseek-ai/dsh-mcp-client` (DSH в developer preview, breaking changes;
  формат записи проверить по `docs/config-catalog.md` нужной версии). Если нет —
  поднять версию DSH.

### 4. Makefile
- Цели: `ddg-mcp` (up -d --no-deps), `ddg-mcp-build`, `ddg-mcp-test`,
  `logs-ddg-mcp`; `ddg-mcp` добавить в `status`.
- Цели DSH: `dsh`, `dsh-build`, `logs-dsh` (как в README раздел 6).
- Решение: ddg-mcp — «сервис по запросу» (как tgbot/scraper/torrent), не в `infra`.

### 5. Тесты
- Standalone (без DSH): JSON-RPC в `POST /mcp`: `initialize` → `tools/list`
  (ждём `search`, `fetch_content`) → `tools/call search` с реальным запросом.
- `make ddg-mcp-test`: контейнерный smoke (аналог `scraper-test`:
  `docker run --rm --network none` + реальный сетевой прогон).
- E2E в DSH: вопрос «найди новости про llama.cpp за неделю» → модель вызывает
  `mcp__ddg__search`, затем `mcp__ddg__fetch_content` и отвечает по результатам;
  trace в Tempo (`service.name=dsh-agent`) — вызовы MCP-tool видны как tool calls.

### 6. Документация
- README-RU.md: новый раздел «Веб-поиск для локальных моделей (DDG MCP)»:
  включение, env-переменные, тесты, лимиты.
- Заодно починить устаревшие ссылки (docker-wsl.sh, `llama` → `qwen`,
  DSH в compose).

## Открытые вопросы (нужно подтверждение)

1. **Точка интеграции — DSH?** dsh удалён из compose.yaml в `8f114d1`. DSH всё
   ещё используется? Включаем восстановление dsh в план, или DSH живёт где-то
   вне стека (тогда только сервис + инструкция по подключению)?
2. **`[browser]` extra (curl_cffi)** — рекомендую «да»: DDG блокирует обычный
   httpx пустым 202, без него поиск может молча возвращать «no results».
3. **Порт хоста 127.0.0.1:8088** — публикуем для отладки или только внутренняя
   сеть compose?
4. **Параметры поиска**: defaults (30/20 RPM, parse mode `text`, без SafeSearch
   и region) — ок? (Русский регион `ru-ru`? SafeSearch off?)
5. **tgbot** — поиск в боте это отдельный проект (tool-loop в `tgbot/chat.py`
   + клиент к `http://ddg-mcp:8000/mcp`). В этот план не входит — подтверждаем?

## Риски

- DSH — developer preview: формат MCP-конфига в закреплённой версии может
  отличаться от master-доков → шаг 3 включает проверку.
- DDG меняет тактики блокировки; mitigation — `auto`-backend с curl_cffi.
- Кэш страниц in-memory: теряется при restart контейнера (не критично).
- LLM-модель (27B Q4) может неровно вызывать инструменты на длинных результатах
  `fetch_content` (8000 знаков) — при E2E проверить, при необходимости
  ограничивать `max_length` в системном промпте.
