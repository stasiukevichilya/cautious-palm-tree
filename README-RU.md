Локальный Qwen3.8-27B: RTX 4070 Ti SUPER 16 GiB + RTX 3080 Ti 12 GiB

## Структура

Один Compose-проект `ml-local` и один `Makefile` в корне: всё запускается и останавливается отсюда
(`make help`). Корневой `compose.yaml` только подключает (`include`) файлы групп, корневой `Makefile` —
их `*.mk`. Пути внутри файла группы — относительно его папки; переменные — из корневого `.env`.

| Папка | Что внутри | Цели |
|---|---|---|
| `llm/` | языковые модели: qwen, qwen-mtp, bonsai-mtp (`bonsai/`), Strata (`strata/src`, отдельный git) | `make qwen`, `qwen-mtp`, `bonsai-mtp`, `strata` |
| `imagegen/` | генераторы sdxl, qwen-image, qwen-image-uc и их API/MCP-фронты images, images-uc | `make sdxl`, `qwen-image`, `qwen-image-uc`, `uc-bonsai`, `images-sync` |
| `agent/` | Open WebUI, терминал агента, прокси сжатия bili и headroom, blender-mcp, бенчмарк сжатия | `make agent`, `agent-functions` |
| `services/` | Telegram-бот, scraper, torrent, miniapps | `make tgbot`, `scraper`, `torrent` |
| `observability/` | Prometheus, Grafana, Tempo, Loki, Alloy, OTel, GPU exporter и их конфиги | `make infra` |

В каждой группе: `compose.yaml`, `<группа>.mk`, исходники сервисов, `scripts/` (загрузка весов, smoke-тесты) и
документация. Данные остаются в корне: веса — `models/`, результаты и состояние — `outputs/`. Скрипты из
`scripts/` и все пути в документации — от корня репозитория.

Qwen-Image 2.1: полный генератор с Heretic GGUF-энкодером на двух GPU,
ComfyUI и HTTP API. [Подготовка и запуск](imagegen/QWEN-IMAGE-RU.md): `make qwen-image`,
интерфейс http://127.0.0.1:8083. Вариант с DiT unsloth Q8_0: `make qwen-image-unsloth`.

Qwen-Image 2.1 Uncensored: отдельный сервис на одной GPU, недоступный боту:
[imagegen/QWEN-IMAGE-UC-RU.md](imagegen/QWEN-IMAGE-UC-RU.md), `make qwen-image-uc`, интерфейс http://127.0.0.1:8085;
`make uc-bonsai` — вместе с bonsai-mtp на двух GPU без CPU-offload.

Агент в браузере для локальной сети: Open WebUI на :1098 со всеми LLM стека, скиллами
Claude Code / pi / opencode, терминалом агента, MCP-генерацией изображений
(sdxl / qwen-image, отдельно qwen-image-uc) и Blender на Windows (blender-mcp): [agent/README-RU.md](agent/README-RU.md), `make agent`.

Telegram-бот для запущенной LLM и генераторов изображений: [services/TGBOT-RU.md](services/TGBOT-RU.md),
`make tgbot`, настройки http://127.0.0.1:8084.

 Мониторинг БУ-объявлений (Kufar, Onliner): новые объявления и падения цен уходят
администраторам в того же Telegram-бота, раз в день — отчёт о ценах (медиана за
неделю, объявления дешевле — жирным). У каждого пользователя Telegram-бота могут
быть свои поисковые запросы (`/query …`) и товары на отслеживании цены по ссылке
(`/watch https://www.kufar.by/item/…`): уведомления приходят только владельцу.
`make scraper`, API http://127.0.0.1:8086 (Bearer TGBOT_ADMIN_KEY):
`POST /api/scrape` — прогон сейчас, `POST /api/report` — отчёт сейчас,
`GET /api/watches` — что отслеживается, `GET /api/items` — отслеживаемые товары.
Состав системного отслеживания: `services/scraper/watch.json` и API; пользовательские
запросы и товары добавляются через бота и хранятся в базе скрейпера.

Скачивание торрентов по magnet-ссылке из Telegram-бота (только администратор):
`/magnet <ссылка>` показывает имя и размер, после подтверждения файл скачивается в
`outputs/torrent`, `/torrents` — текущие загрузки (прогресс, скорость, hash),
`/torrent-pause`/`/torrent-resume` — пауза/возобновление, `/torrent-del <hash>` —
убрать (hash или его уникальное начало).
`make torrent`, API http://127.0.0.1:8087 (Bearer TGBOT_ADMIN_KEY). Отключается
`TGBOT_TORRENT_URL=` в .env. Метрики — в dashboard «Torrent downloads» Grafana.

SDXL Base 1.0: отдельный профиль генерации изображений с HTTP API и веб-интерфейсом.
Подготовка, режимы одной/двух GPU и проверки: [imagegen/SDXL-RU.md](imagegen/SDXL-RU.md).
Запуск после подготовки: `make sdxl`, интерфейс http://127.0.0.1:8082.
Сервис LLM называется `qwen`; управление: `make qwen`, `make models-stop`.
Альтернативы (взаимоисключающие): `make qwen-mtp` (:8081) и `make bonsai-mtp` (:8089) —
Ternary-Bonsai-2-27B Uncensored PQ2_0 + MTP. Bonsai работает только на форке PrismML
llama.cpp, поэтому у него свой образ `llm/bonsai/Dockerfile` (`make bonsai-mtp-build`) и
переменные `BONSAI_MTP_*` (модель, GPU, split, контекст, слоты, MTP, ubatch, кеши) в llm/compose.yaml.
Текущий порт Grafana: http://127.0.0.1:3055.

Текущая конфигурация `qwen`: Qwen3.8-27B Q4_K_M, контекст 196608, K/V q8_0, один слот, layer split 0.60/0.40, ubatch 256, MTP-спекуляция (`--spec-type draft-mtp`, 2 черновых токена; MTP-голова `blk.64.nextn` есть и в Uncensored, и в UD-файле), prompt cache в RAM 4 GiB и 8 context checkpoints. Только текст, mmproj не загружается.

Замер `python3 llm/scripts/bench-qwen.py` (чат-запрос «ревью кода», temperature 0, 768 токенов; 4070 Ti SUPER + 3080 Ti, 03.10.2026):

| Промпт | Генерация без MTP | С MTP (n=2) | Prefill без MTP → с MTP |
|---:|---:|---:|---:|
| 1k | 28.1 t/s | 42.5 t/s (+51%) | 557 → 472 t/s |
| 32k | 23.1 t/s | 41.3 t/s (+79%) | 1189 → 1014 t/s |
| 64k | 18.7 t/s | 32.7 t/s (+75%) | 1062 → 901 t/s |
| 120k | 13.5 t/s | 28.1 t/s (+108%) | 885 → 749 t/s |

Доля принятых черновых токенов 68–82%. Спекуляция не меняет распределение модели (каждый черновой токен проверяет основная модель), но жадный вывод не побайтово совпадает с режимом без MTP: пакетная проверка дает другие округления, и тексты расходятся через сотни символов. MTP-контексту нужно ~1.3 GiB на CUDA1 (KV 408 MiB + compute ~0.9 GiB), поэтому ubatch уменьшен до 256 и split сдвинут на CUDA0; свободно ~0.55 GiB на каждой GPU. n=3 быстрее только на длинном контексте и съедает запас VRAM. Отключить MTP: `QWEN_SPEC_TYPE=none` в .env (тогда можно вернуть QWEN_UBATCH_SIZE=512 и split 0.57,0.43).

Конфигурация `qwen-mtp` (RVN Q4_K_M multilingual + MTP): контекст 163840 целиком в VRAM, K/V и K/V черновика q8_0, ubatch 256, split 0.60/0.40, prompt cache 4 GiB и 8 checkpoints. Контекст выбран с запасом ~1 GiB на каждой GPU. Максимум — 196608 при split 0.62/0.38 (MTP-голова и ее compute-буфер ~0.9 GiB на CUDA1), но тогда на пике свободно лишь ~0.36 / 0.58 GiB. При 0.57/0.43 и ubatch 512 MTP-контекст не создается (OOM на CUDA1). Замер `python3 llm/scripts/bench-qwen.py --url http://127.0.0.1:8081` (05.10.2026):

| Промпт | Prefill | Генерация n=2 | Свободно CUDA0 / CUDA1 (пик) |
|---:|---:|---:|---:|
| 1k | 890 t/s | 67.3 t/s | 1033 / 1094 MiB |
| 64k | 1367 t/s | 40.1 t/s | 1117 / 1094 MiB |
| 120k | 1125 t/s | 32.4 t/s | 1274 / 1094 MiB |
| 160k | 988 t/s | 24.0 t/s | 1043 / 1094 MiB |

По умолчанию n=2 (`QWEN_MTP_SPEC_DRAFT_N_MAX`). При 196608 n=3 давал на 1k 53.1 против 64.7 t/s, на 120k 37.2 против 32.4 t/s: для работы преимущественно с длинным контекстом можно поставить 3.

1. Память и ограничения

Нельзя обеспечить нулевое потребление CPU/RAM. Токенизация, sampling, CUDA-драйвер, сетевые буферы, Docker и мониторинг используют процессор и системную память. Цель — разместить веса, KV и recurrent state в VRAM без CPU-offload больших слоев. CUDA-передачи между картами в WSL также могут использовать RAM. Планируйте несколько GiB RAM для сервисов и дополнительную память при загрузке; жесткий лимит без знания вашего объема RAM не задан.

По config.json: 64 слоя, из них 16 full-attention, 4 KV heads, head_dim=256. Оценка attention KV: C × 16 × 2 × 4 × 256 × bytes_per_element. Для q8_0 коэффициент 34/32 байта с учетом scale-блоков. Recurrent state и служебные буферы считаются отдельно.

| Контекст | KV f16 | KV q8_0 |
|---|---:|---:|
| 32768 | 2 GiB | 1.0625 GiB |
| 65536 | 4 GiB | 2.125 GiB |
| 131072 | 8 GiB | 4.25 GiB |

UD-Q4_K_M: файл около 16.5 GB = 15.37 GiB. Вместе с 128K KV это 19.62 GiB до recurrent state, CUDA context, compute buffers и Windows. Ориентировочный GPU-бюджет процесса 21–24 GiB, но распределение по устройствам и реальные логи важнее суммы. Оставляйте минимум 1–1.5 GiB свободными на каждой GPU под пиковой нагрузкой, больше на карте с монитором.

UD-Q5_K_M около 19.8 GB: альтернатива при уменьшении контекста до 64K. UD-Q6_K около 22 GB: пробовать с 32K/64K только после замеров. Q8_0 около 29 GB: практически не оставляет места кешу и буферам. Не путайте GB на странице загрузки с GiB VRAM. Размеры вариантов UD и обычных K-квантов могут различаться.

2. WSL2 и Docker Desktop

Установите актуальный драйвер NVIDIA в Windows. Linux NVIDIA driver в WSL устанавливать не нужно. В PowerShell выполните `wsl --update` и `wsl -l -v`. В Docker Desktop включите WSL2 engine и интеграцию вашей Ubuntu. Используйте один Docker daemon — в этой инструкции Docker Desktop. Docker Compose должен поддерживать `gpus: all` (2.30+).

В Ubuntu WSL:

```bash
sudo apt-get update
sudo apt-get install -y curl python3 unzip
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.free --format=csv
docker compose version
```

Распакуйте архив в Linux filesystem, например `~/qwen-local`, а не запускайте из `/mnt/c`. Папка с Compose должна стать текущей директорией. Для модели, образов и истории оставьте ориентировочно 50–70 GB свободного SSD.

Запуск Docker выполняется Linux CLI внутри WSL через docker-wsl.sh. Обертка сохраняет выбранный Docker context и переменные DOCKER_HOST/DOCKER_CONTEXT, но использует временный анонимный конфиг без desktop.exe credential helper. Исходный ~/.docker/config.json не изменяется; Windows interop для этих команд не нужен. prepare.sh уже использует обертку автоматически. Для последующих команд используйте `bash ./docker-wsl.sh compose ...`, как показано ниже.

Все скачиваемые образы публичные. Авторизация Docker Hub/GHCR из основного конфига в этом режиме не используется: возможны лимиты анонимных загрузок. Если registry требует login, нужен отдельный Linux credential helper или восстановление штатного Desktop helper; выполнять login через эту временную обертку бессмысленно. Пользовательские proxy/HTTP-header настройки из основного CLI config не копируются. CLI plugins и context metadata доступны через ссылки на исходные каталоги.

Проверка после распаковки:

```bash
bash ./docker-wsl.sh version
bash ./docker-wsl.sh compose version
```

3. Зафиксировать версии и скачать GGUF

Если переходите с предыдущего Q5/64K-комплекта, сохраните существующий .env и named volumes. Обновите файлы комплекта, затем выполните в каталоге стека:

```bash
cp .env ".env.backup-$(date +%Y%m%d-%H%M%S)"
sed -i 's/^QWEN_CTX_SIZE=.*/QWEN_CTX_SIZE=196608/; s/^QWEN_MODEL_FILE=.*/QWEN_MODEL_FILE=Qwen3.8-27B-UD-Q4_K_M.gguf/' .env
bash llm/scripts/download-model.sh
bash ./docker-wsl.sh compose up -d --force-recreate llama
```

Убедитесь, что обе строки есть в .env. prepare.sh повторно запускать не нужно. Старый Q5-файл автоматически не удаляется; для скачивания Q4 требуется еще около 16.5 GB свободного места. Для новой установки используйте команды ниже.


```bash
bash prepare.sh
bash llm/scripts/download-model.sh
```

prepare.sh скачивает образы и сохраняет их digests в .env. Интернет нужен для начальной установки. Это фиксация выбранных версий, а не гарантия их взаимной совместимости. Сохраните .env после приемки; не запускайте автоматические обновления контейнеров.

llm/scripts/download-model.sh скачивает указанный файл из Hugging Face в models/, фиксирует ревизию репозитория и записывает локальную SHA256 (по строке на файл в models/SHA256SUMS). По умолчанию скачивается Qwen3.8-27B-UD-Q4_K_M.gguf; другую модель можно передать явно:

```bash
bash llm/scripts/download-model.sh <repo> <file>
bash llm/scripts/download-model.sh "https://huggingface.co/<owner>/<repo>?show_file_info=<file>.gguf"
```

Например: `bash llm/scripts/download-model.sh JonathanColetti/Qwen3.8-27B-Uncensored-GGUF Qwen3.8-27B-Uncensored-Q4_K_M.gguf`. Ревизия кэшируется отдельно для каждого репозитория в models/.revisions/, поэтому прерванное скачивание можно возобновить даже после обновления репозитория; повторный запуск готового файла пропускает загрузку. Это контроль повторного чтения, не независимая проверка издателя. При необходимости сравните SHA256 с данными выбранной ревизии на Hugging Face. Для gated-репозиториев задайте HF_TOKEN. После скачивания укажите новый файл в .env через QWEN_MODEL_FILE и пересоздайте контейнер llama.

4. Проверить порядок GPU

```bash
bash ./docker-wsl.sh compose run --rm --no-deps llama --list-devices
```

CUDA0 должна быть 4070 Ti SUPER, CUDA1 — 3080 Ti. Если наоборот, измените `CUDA_VISIBLE_DEVICES=1,0` в .env и повторите проверку. Можно указать UUID карт в нужном порядке; сверяйте результат именно с `--list-devices`. `QWEN_TENSOR_SPLIT=0.57,0.43` относится к логическому порядку CUDA.

Предел: VRAM не превращается в единый пул. Неравные размеры слоев, embedding/output и буферов делают 57/43 лишь начальной настройкой. При OOM на CUDA1 и запасе CUDA0 попробуйте 0.60,0.40; при обратной ситуации — 0.54,0.46. Меняйте один параметр за раз.

5. Поднять llama.cpp и проверить размещение

```bash
bash ./docker-wsl.sh compose up -d llama
bash ./docker-wsl.sh compose logs -f llama
```

Дождитесь окончания загрузки. В другом терминале:

```bash
curl --fail http://127.0.0.1:8080/health
python3 llm/scripts/smoke-test.py
nvidia-smi --query-gpu=index,name,memory.used,memory.free,utilization.gpu,power.draw,temperature.gpu --format=csv
```

Проверяйте: обе GPU распознаны, все повторяющиеся слои и output offloaded, KV/recurrent buffers на CUDA, нет предупреждения об отсутствии CUDA и большого CPU model buffer. Одной строки про число слоев недостаточно: input embedding по умолчанию может остаться на CPU. В Compose для него есть явный `--override-tensor token_embd\.weight=CUDA0`. Не удаляйте override, выдавая CPU embedding за полностью GPU-размещение. Если выбранная сборка его отвергает или переносит назад на CPU, это не прошедшая приемку сборка; нужна совместимая версия CUDA backend/GGUF.

`CPU_Mapped`, mmap/page cache и небольшие host/output buffers сами по себе не доказывают CPU-вычисления. Важно, какие тензоры реально размещены на CPU. Не используйте mlock для удержания дополнительной полной копии модели в RAM. В Windows следите также за Shared GPU memory: GPU offload в логах сам по себе не гарантирует отсутствие вытеснения драйвером.

llm/scripts/smoke-test.py проверяет обычный ответ, streaming usage, выдачу tool call и продолжение после результата инструмента. Это обязательнее для агента, чем тест «привет».

6. Поднять мониторинг

```bash
bash ./docker-wsl.sh compose up -d
```

7. Grafana и проверка OpenTelemetry

Откройте http://127.0.0.1:3055, пользователь admin. Пароль — значение GRAFANA_PASSWORD в .env. Источники Prometheus, Tempo и Loki и dashboards LLM (local-qwen), SDXL, Qwen-Image, Qwen-Image UC, Telegram bot, market scraper, torrent и логов provisioned автоматически; между ними есть перекрёстные ссылки.

Схема: OTLP/HTTP → Collector → Tempo (traces), Collector → Prometheus (metrics); llama, sdxl, tgbot, scraper, torrent, ComfyUI-сервисы (через /local-qwen-image*/metrics) и gpu-exporter → Prometheus; Loki ← Alloy ← docker-логи; Grafana читает все хранилища.

После запроса к модели подождите 60–90 секунд:

```bash
curl --fail http://127.0.0.1:8080/metrics
curl --fail http://127.0.0.1:9835/metrics
bash ./docker-wsl.sh compose logs --tail=100 otel tempo
```

В Prometheus http://127.0.0.1:9090/targets все три scrape targets должны быть UP.

Встроенный dashboard показывает вычисленные llama токены, скорость, очередь и доступность. Токены, обработанные движком, могут отличаться от полного usage запроса из-за повторного использования префикса. Не прибавляйте reasoning повторно к completion, если это его подмножество. Для histogram token usage суммируются _sum, не _count.

В Grafana импортируйте официальный GPU dashboard 14574 либо 25547, выбрав Prometheus. Сверьте UUID обеих карт. Некоторые поля WSL могут отсутствовать или быть N/A; это не нулевая нагрузка.

Если WSL не дает VRAM/utilization/power, используйте exporter в Windows как единственное исключение из Docker. Остановите контейнер `bash ./docker-wsl.sh compose stop gpu-exporter`. В PowerShell администратора:

```powershell
winget install --scope machine utkuozdemir.nvidia_gpu_exporter
nvidia_gpu_exporter install
Start-Service nvidia_gpu_exporter
```

В observability/prometheus.yaml замените gpu-exporter:9835 на host.docker.internal:9835 и выполните `bash ./docker-wsl.sh compose restart prometheus`. Проверьте доступ из контейнерной сети. При необходимости разрешите входящий TCP 9835 в Windows Firewall только от сети Docker/WSL. Чтобы обычный `compose up -d` не запускал старый контейнер, добавьте ему `profiles: [wsl-gpu]` в Compose. Если и Windows не отдает отдельный показатель, оставьте его недоступным; мониторинг не создает отсутствующую метрику.

8. Приемка длинного контекста

Сначала короткий smoke test, затем реальные задачи с входом около 32K, 64K, 96K и 115–120K токенов. Измеряйте токены через tokenizer/usage сервера, не количеством символов. Проверяйте длинный prefill и несколько тысяч output tokens, затем повторный ход и tool call. Проведите 30–60 минут под типичной нагрузкой и с обычными Windows-приложениями.

При OOM во время prefill сначала QWEN_UBATCH_SIZE=128; если OOM при создании MTP draft context — уменьшайте QWEN_CTX_SIZE шагами по 8192 или отключите MTP (QWEN_SPEC_TYPE=none). Если тесно всегда — QWEN_CTX_SIZE=65536. Если переполняется только одна карта — корректируйте split. Изменения .env применяются через `bash ./docker-wsl.sh compose up -d --force-recreate llama`. Не компенсируйте OOM уменьшением offload слоев: это нарушит вашу цель. Для 128K уже выбран UD-Q4_K_M. Q5/128K и Q6/64K — эксперимент после замеров, не гарантированный режим. Не задавайте native 262K автоматически.

Prompt cache в RAM (`QWEN_CACHE_RAM`, MiB) и context checkpoints (`QWEN_CTX_CHECKPOINTS`) включены: у гибридной модели recurrent state нельзя откатить, поэтому без них любое расхождение истории означает полный prefill. В многоходовом диалоге на 30K следующий ход обрабатывает только новые токены (7–8 с вместо 38 с). Правка глубоко в истории (раньше последнего checkpoint) по-прежнему пересчитывает промпт с начала. Две карты не обещают двукратного ускорения: layer split выбран для умеренного межкарточного обмена; PCIe topology и WSL влияют на скорость.

9. Постоянная работа

Включите автозапуск Docker Desktop при входе в Windows; отключите сон ПК на питании от сети и Resource Saver Docker Desktop. restart: unless-stopped восстанавливает процессы после старта daemon, но не запускает сам Docker Desktop и не восстанавливает намеренно остановленный контейнер. После перезагрузки выполните health, проверьте обе GPU, metrics и новый trace. Работа после входа пользователя и unattended boot до входа — разные режимы; Docker Desktop не следует считать серверной службой до логина.

Для строгого unattended WSL запуска используйте отдельный вариант Docker Engine в Ubuntu с systemd, NVIDIA Container Toolkit и задачей Windows Task Scheduler под владельцем дистрибутива, которая запускает WSL при старте системы и удерживает его работающим. Не запускайте два daemon для одного стека. Этот архив рассчитан на Docker Desktop; для круглосуточной работы независимо от Windows-сессии обычный Linux надежнее.

Prometheus хранит 30 дней, максимум около 5 GB TSDB (WAL может требовать дополнительное место), Tempo — 7 дней, docker logs ротируются. Alert rules доступны в Prometheus /alerts; внешние уведомления не настроены. Для уведомлений создайте Grafana-managed alerts и contact point: недоступность llama, очередь >10 минут, свободная VRAM <1 GiB на любой GPU. Отдельно задайте пределы температуры с учетом конкретного охлаждения.

Проверка и обслуживание:

```bash
bash ./docker-wsl.sh compose ps
bash ./docker-wsl.sh stats --no-stream
bash ./docker-wsl.sh compose logs --tail=100 llama
bash ./docker-wsl.sh compose restart llama
```

Сохраняйте .env, конфиги, models/revision.txt, models/SHA256SUMS и backup named volumes (для согласованной копии остановите соответствующие сервисы). Не используйте `bash ./docker-wsl.sh compose down -v`: это удалит историю/настройки.

Источники, проверенные 16 сентября 2026:

- Архитектура: https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/config.json
- GGUF и размеры: https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/tree/main
- llama flags и metrics: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Размещение input embedding: https://github.com/ggml-org/llama.cpp/blob/master/src/llama-model.cpp
- Docker CUDA: https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md
- CUDA WSL ограничения: https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- Docker Desktop GPU: https://docs.docker.com/desktop/features/gpu/
- Collector: https://opentelemetry.io/docs/collector/configuration/
- Tempo: https://grafana.com/docs/tempo/latest/configuration/
- GPU exporter: https://github.com/utkuozdemir/nvidia_gpu_exporter
