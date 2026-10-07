# Бенчмарк сжатия контекста: Headroom и billion-context

Итог на 2026-10-07. Прогоны 01:11–04:41, все четыре модели, сырые данные в `outputs/bench/ctx/` (каталог в
`.gitignore`).

## Коротко

- **Headroom (`hr`) стоит включать по умолчанию.** На всех моделях промпт на последнем шаге −34%, prefill за сеанс −34% по
  токенам и −29…−43% по времени, общее время −28…−40%. Все вопросы на память пройдены, ни одной ошибки. Режим
  `HEADROOM_MODE=token` ничего не добавляет к `cache` (по умолчанию).
- **billion-context (`bili`) по умолчанию не включать.** Окно он освобождает сильнее (−44…−59%, если сжатие удалось), но:
  1. сводки пишет сама модель, поэтому prefill за сеанс больше, чем без него, и время больше (+10…+60%);
  2. если просьба о сжатии совпадает с вопросом пользователя, **ответ подменяется служебным текстом bili**
     (`[Compressed …]` или `[Compression FAILED …]`). Так было на qwen, bonsai и Strata, при удачном и неудачном сжатии;
  3. вызов compress генерируется внутри `max_tokens` клиента. При 2048 он обрезается, сжатие срывается
     (`rejected: kind=truncated`). При 32768 bonsai зацикливается до лимита, bili рвёт запрос через ~650 с (HTTP 502),
     а llama.cpp продолжает генерировать брошенный ответ. bonsai/bili с лимитом 32K: 3 таких сбоя, 0 сжатий, 52 мин
     вместо 5;
  4. реальная потеря факта после сводок: bonsai/hb, `retry_backoff_ms` («файл config/payments.toml не читал»).
- **Связка `hb` работает** (headroom → bili проходит, сессия одна). Лучший результат по окну (qwen-mtp: 28K вместо 148K),
  но наследует все проблемы bili. По умолчанию не включать.

## Что сравнивали

| Маршрут | Путь запроса | Open WebUI |
|---|---|---|
| `direct` | модель напрямую | без тумблеров |
| `hr` | Headroom → модель | тумблер **Headroom** (`hr.<model>`) |
| `bili` | billion-context → модель | тумблер **Billion context** (`bili.<model>`) |
| `hb` | Headroom → billion-context → модель | оба тумблера (`hb.<model>`) |

Модели: `qwen3.8-27b-mtp` (окно 163840), `qwen3.8-27b` (196608), `bonsai2-27b-uc-mtp` (262144),
Strata `qwen3.8-flash-next-iq2_xs` (131072).

Бенчмарк `agent/bench/ctx_bench.py`: фиксированная траектория агента из 22 шагов (~403K символов вывода инструментов:
код и конфиги репо, `git log`, JSON трекера, логи k8s, pytest, метрики Prometheus). Модель отвечает на каждом шаге, но
дальше всегда добавляется заранее заданный вызов инструмента, так что все маршруты видят один и тот же сеанс. В конце
4 вопроса на память о фактах из ранних шагов (assignee и label #4187, batch_id при OOM, упавший тест 1374/1375,
`retry_backoff_ms`). Стоимость — дельты счётчиков сервера модели, поэтому служебные запросы bili на сводки учтены.
Thinking выключен, temperature 0.

Колонки таблиц:
- **промпт** — `usage.prompt_tokens` на последнем шаге, то есть занятое окно;
- **prefill** — `session.prompt_tokens_total` / `prompt_seconds_total`: реально обработанные (не из кэша) токены промпта
  за 22 шага, включая сводки bili;
- **wall** — время 22 шагов;
- **recall** — сколько из 4 вопросов отвечено верно;
- **compress** — события в `outputs/agent/bili/state/billion-context/bili.log` по `run_id`: удачные / обрезанные
  (`rejected: kind=truncated`) / оборванные (`network failure`).

## Результаты

### Основной цикл: `max_tokens` 2048 на шагах, 1024 на вопросах

Вопросы на память здесь задаются «вилкой»: каждый отдельно поверх сеанса, без предыдущих вопросов (см. ниже, почему это
важно для `hr`).

**qwen3.8-27b-mtp**

| Маршрут | Промпт | prefill, ток. | prefill, с | wall, с | Recall | compress |
|---|---|---|---|---|---|---|
| direct | 148 476 | 148 560 | 241 | 250 | 4/4 | — |
| hr | 97 546 (−34%) | 97 599 | 141 | 158 | 4/4 | — |
| bili | 60 228 (−59%) | 186 530 | 259 | 368 | 4/4 | 1 / 1 / 0 |
| hb | 28 226 (−81%) | 121 790 | 161 | 222 | 4/4 | 1 / 0 / 0 |

**qwen3.8-27b**

| Маршрут | Промпт | prefill, ток. | prefill, с | wall, с | Recall | compress |
|---|---|---|---|---|---|---|
| direct | 148 474 | 148 558 | 241 | 250 | 4/4 | — |
| hr | 97 544 (−34%) | 97 628 | 141 | 156 | 4/4 | — |
| bili | 70 237 (−53%) | 177 525 | 238 | 274 | 4/4 | 1 / 0 / 0 |
| hb | 101 512 (−32%) | 103 556 | 152 | 225 | 2/4 ¹ | 0 / 3 / 0 |

**bonsai2-27b-uc-mtp**

| Маршрут | Промпт | prefill, ток. | prefill, с | wall, с | Recall | compress |
|---|---|---|---|---|---|---|
| direct | 148 477 | 149 036 | 314 | 308 | 4/4 | — |
| hr | 97 550 (−34%) | 98 270 | 179 | 185 | 4/4 | — |
| bili | 152 446 (+3%) | 158 410 | 342 | 498 | 4/4 | 0 / 3 / 0 |
| hb | 101 518 (−32%) | 103 562 | 192 | 247 | 3/4 ¹ | 0 / 2 / 1 |

¹ Не потеря факта: вместо ответа пришло служебное сообщение bili `[Compression FAILED: … not parseable JSON (looks
truncated) …]`. Вызов compress не уместился в `max_tokens` 1024.

qwen и qwen-mtp на prefill одинаковы (MTP ускоряет только генерацию, а здесь почти всё время — prefill). Prefill bonsai
на ~30% медленнее.

### Как у реальных клиентов: bonsai, `max_tokens` 32768, вопросы цепочкой

pi и opencode шлют `max_tokens` 32768 / 32000, Open WebUI по умолчанию не ограничивает. Вопросы идут друг за другом с
ответами в истории, как в чате. Файлы `*-real.json`, `*-token.json`.

| Маршрут | Промпт | prefill, ток. | prefill, с | wall, с | Recall | compress | HTTP-ошибки |
|---|---|---|---|---|---|---|---|
| direct | 148 477 | 149 036 | 313 | 305 | 4/4 | — | 0 |
| hr | 97 550 | 98 272 | 179 | 185 | 4/4 | — | 0 |
| hr, `HEADROOM_MODE=token` | 97 562 | 98 280 | 180 | 196 | 4/4 | — | 0 |
| bili | 152 446 | 395 410 | 1 361 | **3 148** | 4/4 | 0 / 0 / 3 | 3 × 502 |
| hb | 33 420 (−77%) | 126 990 | 212 | 377 | 3/4 ² | 1 / 0 / 0 | 0 |
| hb, `HEADROOM_MODE=token` | 94 600 | 188 030 | 379 | 558 | 4/4 | 1 / 0 / 0 ³ | 0 |

² Настоящая потеря: на вопрос о `retry_backoff_ms` модель ответила, что `config/payments.toml` не читала. bili свернул
контекст в 11 сводок, факт в них не попал.

³ Свёрнуты только первые 3 сообщения (~10.5K). В режиме `token` Headroom каждый раз переписывает прошлые ходы, и bili
видит нестабильную историю.

**bonsai + bili, лимит 32K.** На просьбу о сжатии модель генерирует до лимита (12K, 22K, … токенов при ~25–37 ток/с). bili
ждёт ~650 с и отдаёт 502 с `network failure … armed emergency shrink`. Брошенная генерация занимает слот llama.cpp,
следующий запрос идёт в другой слот и пересчитывает весь промпт заново (137K токенов, ~9 мин). Аварийное сжатие так и не
сработало.

### Strata (`qwen3.8-flash-next-iq2_xs`): 13 шагов, `max_tokens` 8192, вопросы цепочкой

Окно 131072, поэтому траектория урезана до 13 шагов (~112K на последнем). `max_tokens` 8192 вместо 32768, чтобы
возможное зацикливание на сжатии не стоило часы на медленной модели. Счётчики — из JSON `/metrics` Strata (`totals`).

| Маршрут | Промпт | prefill, ток. | prefill, с | wall, с | Recall | compress |
|---|---|---|---|---|---|---|
| direct | 112 142 | 112 226 | 413 | 387 | 4/4 | — |
| hr | 75 504 (−33%) | 75 588 | 294 | 280 | 4/4 | — |
| bili | 62 284 (−44%) | 155 488 | 522 | 557 | 3/4 ⁴ | 2 / 0 / 0 |
| hb | 81 383 (−27%) | 81 467 | 310 | 336 | 3/4 ⁴ | 2 / 0 / 0 |

⁴ Сжатие удалось, но ответ на вопрос заменён отчётом bili `[Compressed m00012–m00023 → 1 block(s), ~32295 tokens saved.] …
No compressible ranges remain …`.

С `max_tokens` 8192 bili считает окно 91 808 и начинает сжимать уже на ~72K.

## Почему `hr` не сжимал вопросы на память

Это особенность бенчмарка, а не Headroom. В режиме `cache` (`headroom/proxy/handlers/openai.py`,
`_strict_previous_turn_frozen_count`) изменяемым считается только последнее сообщение, если это `user`/`tool`. Всё
остальное «заморожено», а уже сжатые прошлые ходы Headroom переотправляет только когда новый запрос продолжает
предыдущий: те же сообщения плюс новые в конце (`overlay_cached_prefix` в `headroom/cache/prefix_tracker.py`).

В основном цикле каждый вопрос задавался отдельно поверх сеанса. Вопрос 1 продолжает последний шаг, и сжатие работает
(98.5K). Вопросы 2–4 — соседние ветки, а не продолжение, поэтому уходят несжатыми (149.6K, заголовки
`x-headroom-tokens-saved: 0`). В чате вопросы идут друг за другом. С `--recall chain` сжаты все четыре (98.5–98.8K),
`recall_cost` 1K токенов вместо 150K.

`HEADROOM_MODE=token` на этой траектории ничего не даёт для `hr` (те же 97.5K) и мешает bili в `hb` (см. выше).

## Рекомендации по умолчанию (применены 2026-10-07)

| Клиент | Headroom | Billion context |
|---|---|---|
| Open WebUI | включён по умолчанию | выключен, включать вручную для очень длинных чатов на qwen |
| pi | через Headroom | не подключён |
| opencode | через Headroom | не подключён |

- **Open WebUI.** Тумблер Headroom включён в новых чатах: `"defaultFilterIds": ["headroom"]` в
  `DEFAULT_MODEL_METADATA` (compose и живой конфиг через `POST /api/v1/configs/models`). Billion context ручной:
  он вырезает ответ, когда сжимает на вопросе, а на bonsai с неограниченным выводом может зависнуть на ~11 мин.
- **pi и opencode.** Headroom опубликован на `127.0.0.1:8091`. Провайдеры в `~/.pi/agent/models.json` и
  `~/.config/opencode/opencode.jsonc` ходят на `http://127.0.0.1:8091/v1` с заголовком
  `x-headroom-base-url: http://<qwen|qwen-mtp|bonsai-mtp>:8080`. Копии до правки: `*.bak-20261007-102231`.
  Проверено `opencode run` и `pi -p`: запросы идут через Headroom, thinking и `thinking_budget_tokens` доходят.
  bili для них не подходит: `max_tokens` 32K, и на bonsai это даёт зацикливание из таблицы выше.
- `HEADROOM_MODE` по умолчанию (`cache`). Окна моделей заданы в `HEADROOM_MODEL_LIMITS`, иначе Headroom считает
  незнакомые модели 128K.

## Проверка в Open WebUI (2026-10-07, qwen-mtp)

| Сценарий | Маршрут по логам | Что видно в чате |
|---|---|---|
| новый чат, тумблеры по умолчанию | Headroom → qwen-mtp | Headroom вкл., Billion context выкл.; плашка Headroom, живые `timings`, панель контекста с Headroom |
| оба тумблера | Headroom → bili (`owui-<chat_id>`) → qwen-mtp | плашки billion-context и Headroom; `timings` нет (bili их срезает), только токены |
| Headroom выключен | напрямую в qwen-mtp | без плашки, `timings` есть |
| контейнер `headroom` остановлен | напрямую (после исправления) | статус «Headroom недоступен, запрос идёт без Headroom», без плашки |

Найдено и исправлено:
- при остановленном `headroom` и включённом тумблере чат падал с «Open WebUI: Server Connection Error»: копии `hr.*`
  остаются в списке моделей. Фильтр `headroom.py` (1.1.0) теперь проверяет `/health` (кэш 15 с) и идёт в обход;
- плашка Headroom ставилась по состоянию тумблера, а не по маршруту. Фильтр Headroom отмечает пропущенные чаты в
  `app.state.headroom_skipped`, `context_usage.py` (2.4.0) по ним плашку не ставит.

Не связано с Headroom: ответ появляется в интерфейсе через 25–35 с, хотя модель отвечает за 1–5 с (видно и без
Headroom). В логе Open WebUI при этом `KeyError: 'model'` в `run_initial_title_generation`. Не разбиралось.

## Как воспроизвести

```bash
agent/bench/all.sh                                      # qwen-mtp -> qwen -> bonsai-mtp, 4 маршрута, max_tokens 2048/1024
agent/bench/run.sh --model bonsai2-27b-uc-mtp --max-tokens 32768 --recall chain --tag=-real
agent/bench/run.sh --model qwen3.8-flash-next-iq2_xs --steps 13 --max-tokens 8192 --recall chain   # после make strata
python3 agent/bench/summarize.py qwen3.8-27b-mtp qwen3.8-27b bonsai2-27b-uc-mtp qwen3.8-flash-next-iq2_xs
```

Флаги `ctx_bench.py`:
- `--recall fork|chain` — вопросы отдельно поверх сеанса (по умолчанию) или цепочкой, как в чате;
- `--max-tokens N` — `max_tokens` на шагах и вопросах (по умолчанию 2048 / 1024);
- `--headroom-url` — другой экземпляр Headroom, например временный с `HEADROOM_MODE=token`;
- `--tag` — суффикс имени файла (с ведущим `-` писать через `=`: `--tag=-real`).

Режим `token` мерили на временном контейнере с тем же образом в сети `ml-local_default`. Рабочий `headroom` не
трогали.

Результаты: `outputs/bench/ctx/<model>-<route><tag>.json` (`steps[].usage`, `steps[].headroom`, `session`, `recall[]`,
`recall_cost`, `recall_mode`, `max_tokens`) и логи `<model>.log`, `bonsai2-27b-uc-mtp-real.log`, `-token.log`. Файлы
`run1-*`, `run2-*`, `smoke-*` — прогоны до этого цикла на старой нагрузке.

## Что ещё не проверено

- Можно ли настроить bili так, чтобы просьба о сжатии не приходилась на ответ пользователю и не подменяла его.
- Задержка показа ответа в Open WebUI (см. выше).
- Длинная агентная сессия pi / opencode через Headroom (проверены короткие запросы).

## Попутные изменения

- `Makefile`: `images` и `images-uc` теперь следуют за генераторами (`make images-sync` в конце целей моделей, в
  `models-stop` и `agent`). `images` работает при sdxl/qwen-image, `images-uc` — при sdxl/qwen-image/qwen-image-uc.
  Описано в `agent/README-RU.md`.
- `agent/bench/ctx_bench.py`: флаги выше и разбор JSON `/metrics` Strata. Новый `agent/bench/summarize.py`.
- `compose.yaml`: порт `127.0.0.1:8091` и `HEADROOM_MODEL_LIMITS` у `headroom`, `defaultFilterIds` у Open WebUI.
- `agent/functions/headroom.py`, `context_usage.py`: обход недоступного Headroom и честная плашка.
