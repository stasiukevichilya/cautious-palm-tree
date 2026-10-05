# План: Strata (Qwen3.8-Flash-Next 125B MoE) в docker-стеке на двух GPU

Цель: запустить [Strata](https://github.com/Niko1221/Strata) как сервис профиля `strata` в
`compose.yaml` (рядом с `qwen` / `qwen-mtp` / `bonsai-mtp`), модель **Qwen3.8-Flash-Next
IQ2_XS**, обе GPU через layer split, затем подобрать настройки замерами.

Основной источник — `docs/AI_SETUP.md` репозитория (написан для агентов: шаги, флаги,
проверки, таблица ошибок). Про две GPU — `docs/MULTI_GPU.md`, `docs/SECOND_GPU.md`,
`docs/BATCHING.md`, WSL-ограничения — `docs/DETAILS.md` (раздел Linux / WSL).
На момент составления: `main` @ `6f32ec0` (04.10.2026), движок v0.1.39 (тег `v0.1.39`).

## Решения пользователя (05.10.2026)

1. Модель: `--family qwen --model IQ2_XS` (рекомендована для 64 GB; эксперты ~35.5 GB в RAM,
   скачивание ~70 GB + MTP-слой ~6 GB при первом старте).
2. Память WSL поднимаем до ~52 GB.
3. Скачивание ~76 GB — согласовано.
4. Запуск в Docker (сервис в нашем compose), не нативный `./setup.sh` и не Windows `START-HERE.bat`.

## Что такое Strata (коротко)

Собственный движок (C++/CUDA поверх ggml на закреплённом коммите llama.cpp) + Python-сервер
`serve/server.py`. MoE-модель: плотные веса и кэш самых частых экспертов — в VRAM, все
эксперты — в RAM (арена), промахи кэша считает CPU-пул; второй шард (lookup-таблица 29 GB)
остаётся на диске. MTP-спекуляция встроена. API: OpenAI `/v1`, Anthropic `/v1/messages`,
Responses `/v1/responses`, веб-UI (Chat / Monitor / About) на том же порту, `/health`,
`/metrics` (JSON, не Prometheus), `POST /unload`, `POST /load`.

## Железо и ограничения (проверено)

| | Значение |
|---|---|
| GPU0 | RTX 4070 Ti SUPER 16 GB, sm 8.9, PCIe 4.0 x16 |
| GPU1 | RTX 3080 Ti 12 GB, sm 8.6, **PCIe 4.0 x4** |
| Драйвер | 616.56 (нужно ≥580) |
| CPU | Ryzen 9 7950X3D (AVX2), в WSL 32 потока |
| RAM | хост 63.6 GB; WSL/Docker Desktop сейчас видят **31 GB** (дефолт 50%) |
| Диск | `/` (ext4 WSL) ~760 GB свободно |
| Docker | Docker Desktop 29.8.2, runtime `nvidia` есть |

Ограничения WSL2 (из доков Strata):
- WDDM: из арены экспертов закрепляется (pin) максимум 8 GiB, остальное идёт через
  pinned staging ring → промпт медленнее, чем на нативном Linux (#253).
  `STRATA_ARENA_PIN_GIB=N` меняет лимит, но больше 8 GiB WDDM может начать отказывать в аллокациях.
- KV streaming (`--kv-resident`) под WSL невозможен → весь KV в VRAM, длинный контекст
  отнимает VRAM у кэша экспертов.
- P2P между GeForce под WSL нет → `--peer-device` не применим.
- `setup.py` читает RAM из `/proc/meminfo` — в контейнере это RAM VM WSL, не лимит контейнера.
  Поэтому лимит памяти контейнеру не ставим, `LOW_RAM=off` явно.

Конфликты с текущим стеком:
- Обе GPU сейчас заняты `qwen-mtp` (по ~15.7 / 11.7 GB) → Strata взаимоисключающая со всеми
  моделями (`qwen`, `qwen-mtp`, `bonsai-mtp`, `sdxl`, `qwen-image`, `qwen-image-uc`).
- Порты 8080–8087, 8089 заняты → Strata на **127.0.0.1:8090**.
- RAM: при 52 GB у WSL и ~40 GB под Strata остальной стек (Grafana/Loki/Tempo/Prometheus,
  tgbot, scraper, torrent, kind-кластер `local-dev-*`) должен уместиться в ~10 GB; при
  нехватке — останавливать kind-кластер на время работы Strata.

## Шаг 0. Подготовка (делает пользователь, агент только подсказывает)

1. `C:\Users\<user>\.wslconfig`, секция `[wsl2]` уже содержит `networkingMode=mirrored`,
   `dnsTunneling=true`, `autoProxy=true`, `firewall=true`. Добавить:
   ```ini
   memory=52GB
   swap=16GB
   ```
2. В PowerShell: `wsl --shutdown`, затем снова запустить Docker Desktop (перезапустится весь
   стек: `make infra` и нужные сервисы).
3. Проверка в WSL: `free -g` ≈ 51 GB total; `docker info | grep "Total Memory"` ≈ 51 GiB.
4. Файл подкачки Windows — «System managed» (по таблице ошибок AI_SETUP: иначе
   `ExpertCache: cudaMalloc(...) out of memory` при свободной VRAM).

## Шаг 1. Исходники и образ

- Исходники: git-сабмодуль не нужен; клонировать в `./strata/src` (добавить в `.gitignore`)
  на тег `v0.1.39`:
  `git clone --branch v0.1.39 --depth 1 https://github.com/Niko1221/Strata strata/src`.
- Сборка их `Dockerfile` (база `nvidia/cuda:13.0.0-devel-ubuntu24.04`, движок компилируется
  при `docker build`, модель качается при первом старте):
  - `CUDA_ARCHITECTURES=86;89` (только наши карты, быстрее сборка);
  - `BUILD_VISION=0` (картинки не нужны; пересобрать с `1`, если понадобятся).
- Тег образа: `local/strata:0.1.39`.
- Собственный `strata/Dockerfile` не пишем, пока хватает их; обновление = `git -C strata/src
  fetch --tags && checkout vX`, затем `make strata-build`.

## Шаг 2. Сервис `strata` в `compose.yaml`

Оформить как `bonsai-mtp` (якорь `*common`, отдельный профиль, комментарий с замерами сверху).
Черновик:

```yaml
  strata:
    # Qwen3.8-Flash-Next 125B MoE (Strata engine, github.com/Niko1221/Strata), IQ2_XS.
    # Experts live in RAM (~35.5 GB), hot experts cached in VRAM of both GPUs (layer split).
    # WSL2: only 8 GiB of the expert arena is pinned, KV stays in VRAM (no KV streaming).
    <<: *common
    profiles: ["strata"]
    build:
      context: ./strata/src
      args:
        CUDA_ARCHITECTURES: "86;89"
        BUILD_VISION: "0"
    image: local/strata:0.1.39
    gpus: all
    ipc: host
    ulimits:
      memlock: -1
    environment:
      FAMILY: qwen
      MODEL: ${STRATA_MODEL:-IQ2_XS}
      CONTEXT: ${STRATA_CONTEXT:-65536}
      VISION: "no"
      KV: ${STRATA_KV:-int8}
      GPUS: ${STRATA_GPUS:-0,1}
      LAYER_SPLIT: ${STRATA_LAYER_SPLIT:-}        # empty = auto
      LOW_RAM: "off"
      REINSTALL: ${STRATA_REINSTALL:-0}
      # opt-in engine knobs, see "Шаг 6"
      STRATA_STAGE_TRIM: ${STRATA_STAGE_TRIM:-}
      STRATA_PREFILL_HELP: ${STRATA_PREFILL_HELP:-}
      STRATA_SPLIT_OWN: ${STRATA_SPLIT_OWN:-}
      STRATA_ARENA_PIN_GIB: ${STRATA_ARENA_PIN_GIB:-}
    volumes:
      - ./models/strata:/data
    ports:
      - 127.0.0.1:8090:8080
    stop_grace_period: 75s
```

Проверить при реализации:
- `restart: unless-stopped` из `*common` + healthcheck образа (`start-period=600s`): на первом
  старте идёт скачивание ~76 GB дольше 10 минут. Убедиться, что Docker Desktop не перезапускает
  контейнер по unhealthy (compose сам не рестартит по health, но проверить); при необходимости
  переопределить `healthcheck.start_period` большим значением на время первого старта.
- Пустые `STRATA_*` переменные: убедиться, что движок трактует пустую строку как «не задано»;
  если нет — передавать их через `env_file`/`.env` только когда нужны.
- `API_KEY` не задаём: порт слушается только на 127.0.0.1 (правило AI_SETUP: без ключа — не наружу).
  Если боту/piweb нужен доступ из docker-сети — он идёт по имени сервиса `strata:8080` внутри
  сети compose, это не наружу; если когда-либо понадобится `0.0.0.0` на хосте — только с `API_KEY`.
- Конфиг установки лежит в `./models/strata/config/strata-iq2_xs.json`; движковые флаги —
  в ключе `"args"` (список), прочие ключи: `gpu`, `layer_split`, `parallel`, `idle_unload_s`,
  `remote_expert_opt`, `split_skip_if_fits`, `min_free_vram_mib`, `before_load`. Правка → рестарт.

## Шаг 3. Makefile

- `MODEL_PROFILES` += `--profile strata`.
- В `.PHONY` и help: `strata`, `strata-build`, `logs-strata`.
- Цели:
  ```make
  strata-build:
  	$(COMPOSE) --profile strata build strata

  strata: infra
  	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image qwen-image-uc
  	$(COMPOSE) --profile strata up -d --no-deps strata

  logs-strata:
  	$(COMPOSE) logs --tail=100 -f strata
  ```
- Добавить `strata` в списки `stop -t 75 ...` всех остальных целей запуска моделей
  (qwen, qwen-mtp, bonsai-mtp, sdxl, sdxl-dual, qwen-image*, uc-bonsai) и в `models-stop`.
- `.env.example` (если есть) / README: переменные `STRATA_*`.

## Шаг 4. Первый старт и проверка (AI_SETUP, шаги 5–7)

1. `make strata-build` (компиляция движка, долго; запускать в фоне, опрашивать вывод).
2. `make strata` → entrypoint вызывает `setup.py --setup --yes --family qwen --model IQ2_XS
   --context 65536 --vision no --gpus 0,1 --low-ram off --no-start ...`, качает модель в
   `./models/strata`, готовит pack, затем `exec setup.py` → сервер (PID 1).
   Скачивание возобновляемое; не убивать контейнер из-за тишины в логах.
3. При старте модели система может «подвисать» 1–3 минуты (загрузка 35+ GB в RAM, pin) — норма.
4. В логе найти и записать: выбор layer split (`layer split auto: K=..`), число экспертов
   в VRAM на каждой карте, предупреждения про pin/WDDM, рекомендацию `parallel`.
5. Проверки:
   ```sh
   curl -s http://127.0.0.1:8090/health        # "loaded": true
   curl -s http://127.0.0.1:8090/v1/models
   curl -s http://127.0.0.1:8090/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"strata","messages":[{"role":"user","content":"Say hello in five words."}],"max_tokens":64,"reasoning_effort":"none"}'
   ```
   Плюс короткий запрос на русском (качество вне английского) и tool call (как в `smoke-test.py`).
6. `nvidia-smi` — обе карты заняты; `free -g` — запас RAM.

## Шаг 5. Базовый замер

- `bench-qwen.py` параметризовать URL/моделью (или скопировать в `bench-strata.py`), прогнать
  1k / 32k / 64k промпты, temperature 0, 768 токенов: генерация t/s, prefill t/s.
- Параллельно смотреть `curl -s :8090/metrics` (JSON: per-request timings, MTP acceptance,
  CPU/GPU-эксперты).
- Сравнить с таблицей `qwen` (Qwen3.8-27B MTP) из README-RU.md.
- Каждый замер ниже — минимум 2 повтора, одинаковые запросы.

## Шаг 6. Две GPU: варианты и порядок экспериментов

Менять по одному параметру, фиксировать результат в таблице в `STRATA-RU.md`.

1. **Layer split auto (база).** `GPUS=0,1`: 4070 Ti S первая (новее, x16; последняя карта
   получает output head и MTP-слой ~0.8 GB — это будет 3080 Ti). Каждая карта кэширует эксперты
   своих слоёв; между картами — только активации раз за verify-окно, x4 у 3080 Ti почти не важен.
   Ожидание по их замерам: prefill +18–20% к одной карте, decode ≈ быстрой карте.
   Сравнить с `GPUS=` пусто + `GPU=0` (только 4070 Ti S) — контроль.
   Опционально проверить обратный порядок `1,0`.
2. **`STRATA_STAGE_TRIM=1` + явный `LAYER_SPLIT=K`** (K из лога auto, затем K±2). Каждая
   карта грузит плотные веса только своих слоёв (иначе полная копия на каждой), освободившаяся
   VRAM идёт под эксперты; у авторов +6% decode. Работает только с явным K, не с `auto`.
3. **`STRATA_PREFILL_HELP=1`** — простаивающая карта помогает на промптах ≤~3.3K токенов
   (у авторов +20–45% prefill на 1–3K). Типичный сценарий бота/piweb. Не бит-в-бит с
   дефолтом (повторяемо). Несовместим с `STRATA_PF_FUSED=1`.
4. **`STRATA_SPLIT_OWN=auto`** (или `1`) — собственные prompt-буферы на каждой карте:
   быстрее prefill, но другой набор резидентных экспертов.
5. **`STRATA_ARENA_PIN_GIB`** — попробовать 12/16 вместо 8 под WSL; при ошибках аллокации
   вернуть. Смотреть prefill на 32k.
6. **Альтернатива split: helper-кэш на 3080 Ti.** Без `GPUS`, `GPU=0`, в `"args"` конфига:
   `--expert-cache auto --expert-cache-device1 auto --remote-expert-opt` (CUDA0 делает всё,
   CUDA1 хранит доп. эксперты и считает их строки в decode; prefill только на CUDA0).
   Взаимоисключающе с `--layer-split`. A/B против лучшего split.
7. **`--calibrate`** (NVIDIA-only, 5–10 мин, пишет лучшие значения в конфиг): в контейнере
   `docker compose exec strata .venv/bin/python setup.py --calibrate` (сервер при этом
   остановить / проверить, как calibrate уживается с работающим сервером; при необходимости
   отдельный `docker compose run --rm strata ...`).
8. **Контекст.** Старт 65536 (int8 KV). Затем 131072 — сравнить decode/prefill и число
   экспертов в VRAM; `KV=q4_0` вдвое уменьшает KV (+~4% скорости на 128K, но perplexity +8–12%)
   — только если 128K нужен и не влезает. Менять через `STRATA_REINSTALL=1` или правкой конфига.
9. **`parallel: 2`** — для одновременной работы tgbot + piweb. Слоты едят VRAM у кэша, запрос
   в слоте идёт без MTP; включать, только если замер показывает приемлемую потерю одиночной
   скорости. `--batch-groups` с split — см. BATCHING.md.
10. **`split_skip_if_fits`** — не нужен (16 GB не вмещает все эксперты IQ2_XS), не трогать.
11. **Не применимо:** `--peer-device` (нет P2P), `--kv-resident` (WSL), `--mmap-experts`
    (только canonical pack; не нужен при достаточной RAM).

## Шаг 7. Интеграция (после стабильного запуска)

- **tgbot / piweb:** добавить `strata` как ещё одну OpenAI-совместимую модель (по аналогии с
  `bonsai-mtp`; внутри сети compose — `http://strata:8080/v1`). Модель можно звать любым именем.
  `reasoning_effort` по умолчанию `high` — для бота, вероятно, `low`/`none`.
- **Сосуществование с генерацией картинок:** в конфиг `idle_unload_s` (напр. 600),
  `min_free_vram_mib`; `POST /unload` / `/load` для ручного переключения. Решить, нужно ли
  это или достаточно взаимоисключающих make-целей.
- **Мониторинг:** GPU уже видны через `gpu-exporter`. `/metrics` Strata — JSON; при желании
  маленький адаптер → Prometheus (tok/s, prefill, MTP acceptance, эксперты GPU/CPU) и панель
  в `grafana/dashboards/inference.json`. Опционально.
- **Claude Code / pi через Anthropic API:** `ANTHROPIC_BASE_URL=http://127.0.0.1:8090`.

## Шаг 8. Документация

- `STRATA-RU.md`: подготовка (.wslconfig), сборка, запуск, переменные `STRATA_*`, где лежат
  данные, таблица замеров, выбранные настройки, известные ограничения WSL.
- Строка в `README-RU.md` (рядом с bonsai-mtp): `make strata`, http://127.0.0.1:8090.
- После реализации этот план удалить или пометить выполненным.

## Запасной вариант

Если в WSL всё упрётся в pin/WDDM или RAM: нативный Windows `START-HERE.bat --yes --family qwen
--model IQ2_XS --gpus 0,1 --port 8090 --no-start` (видит все 64 GB; WDDM-лимит pin тот же) —
вне docker-стека, доступ из контейнеров через `host.docker.internal:8090`.

## Таблица ошибок (из AI_SETUP)

| Симптом | Действие |
|---|---|
| Скачивание/установка оборвалась | перезапустить контейнер — продолжит |
| Очень медленно, диск занят, «engine stopped unexpectedly» | не хватает RAM: проверить `free -g`, остановить лишнее (kind), меньший размер — `Q2_0` |
| `cudaMalloc(...) out of memory` при свободной VRAM | подкачка Windows выключена/мала → «System managed» |
| `prompt ... exceeds the context` | увеличить `STRATA_CONTEXT` + `STRATA_REINSTALL=1` |
| Порт занят | другой `STRATA` порт в compose |

Логи: `make logs-strata`; лог движка `strata-iq2_xs.log` в `/opt/strata` контейнера.
Если не решается — собрать лог + вывод setup, issue в github.com/Niko1221/Strata/issues.
