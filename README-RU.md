Локальный Qwen3.8-27B: RTX 4070 Ti SUPER 16 GiB + RTX 3080 Ti 12 GiB

SDXL Base 1.0: отдельный профиль генерации изображений с HTTP API и веб-интерфейсом.
Подготовка, режимы одной/двух GPU и проверки: [SDXL-RU.md](SDXL-RU.md).
Запуск после подготовки: `make sdxl`, интерфейс http://127.0.0.1:8082.
Текущие сервисы LLM называются `qwen` и `gemma`; управление: `make qwen`,
`make gemma`, `make models-stop`. Текущий порт Grafana: http://127.0.0.1:3055.

Рекомендуемый старт: Unsloth UD-Q4_K_M, контекст 131072, K/V q8_0, один слот, layer split 0.57/0.43. Только текст; mmproj и speculative/MTP не загружаются. Это расчетная конфигурация, которую нужно принять по результатам проверки на вашем ПК. Здесь проверен синтаксис Compose и Python; запуск модели и совместимость контейнеров на GPU не проверялись.

1. Память и ограничения

Нельзя обеспечить нулевое потребление CPU/RAM. Токенизация, sampling, CUDA-драйвер, сетевые буферы, Docker, harness, его команды и мониторинг используют процессор и системную память. Цель — разместить веса, KV и recurrent state в VRAM без CPU-offload больших слоев. CUDA-передачи между картами в WSL также могут использовать RAM. Планируйте несколько GiB RAM для сервисов и дополнительную память при загрузке; жесткий лимит без знания вашего объема RAM не задан.

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
sed -i 's/^QWEN_CTX_SIZE=.*/QWEN_CTX_SIZE=131072/; s/^QWEN_MODEL_FILE=.*/QWEN_MODEL_FILE=Qwen3.8-27B-UD-Q4_K_M.gguf/' .env
bash download-model.sh
bash ./docker-wsl.sh compose up -d --force-recreate llama
```

Убедитесь, что обе строки есть в .env. В DSH измените Context window существующей модели на 131072, оставьте Max output tokens=8192 и начните новую сессию. prepare.sh повторно запускать не нужно. Старый Q5-файл автоматически не удаляется; для скачивания Q4 требуется еще около 16.5 GB свободного места. Для новой установки используйте команды ниже.


```bash
bash prepare.sh
bash download-model.sh
```

prepare.sh скачивает образы и сохраняет их digests в .env; DSH получает точную опубликованную npm-версию 0.1.x. Интернет нужен для начальной установки. Это фиксация выбранных версий, а не гарантия их взаимной совместимости. Сохраните .env после приемки; не запускайте автоматические обновления контейнеров. Для полного восстановления сохраните и собранный образ DSH, поскольку зависимости npm могут использовать диапазоны версий.

download-model.sh фиксирует Hugging Face revision и записывает локальную SHA256. Это контроль повторного чтения, не независимая проверка издателя. При необходимости сравните SHA256 с данными выбранной ревизии на Hugging Face.

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
python3 smoke-test.py
nvidia-smi --query-gpu=index,name,memory.used,memory.free,utilization.gpu,power.draw,temperature.gpu --format=csv
```

Проверяйте: обе GPU распознаны, все повторяющиеся слои и output offloaded, KV/recurrent buffers на CUDA, нет предупреждения об отсутствии CUDA и большого CPU model buffer. Одной строки про число слоев недостаточно: input embedding по умолчанию может остаться на CPU. В Compose для него есть явный `--override-tensor token_embd\.weight=CUDA0`. Не удаляйте override, выдавая CPU embedding за полностью GPU-размещение. Если выбранная сборка его отвергает или переносит назад на CPU, это не прошедшая приемку сборка; нужна совместимая версия CUDA backend/GGUF.

`CPU_Mapped`, mmap/page cache и небольшие host/output buffers сами по себе не доказывают CPU-вычисления. Важно, какие тензоры реально размещены на CPU. Не используйте mlock для удержания дополнительной полной копии модели в RAM. В Windows следите также за Shared GPU memory: GPU offload в логах сам по себе не гарантирует отсутствие вытеснения драйвером.

smoke-test.py проверяет обычный ответ, streaming usage, выдачу tool call и продолжение после результата инструмента. Это обязательнее для агента, чем тест «привет». Затем в самом DSH отдельно проверьте небольшой цикл чтения/создания файла.

6. Поднять мониторинг и DeepSeek Harness

```bash
bash ./docker-wsl.sh compose build dsh
bash ./docker-wsl.sh compose run --rm --no-deps dsh dsh plugin --profile web add @loongsuite/dsh-plugin@0.1.2
bash ./docker-wsl.sh compose up -d
bash ./docker-wsl.sh compose logs --tail=100 dsh
```

Если plugin manager именно выбранной версии не принимает version suffix, используйте документированное `dsh plugin --profile web add @loongsuite/dsh-plugin`, проверьте установленную версию в логах и сохраните состояние dsh-data. Не считайте отсутствие ошибки установки доказательством работающей телеметрии.

Откройте напечатанный DSH URL с токеном авторизации, обычно http://127.0.0.1:3080. DSH слушает loopback внутри контейнера; соседний socat разделяет его network namespace и пересылает 3081 → 3080. На хост опубликован только loopback. Это учитывает документированный запрет DSH на bind 0.0.0.0. После пересоздания контейнера DSH пересоздавайте и dsh-forward: `bash ./docker-wsl.sh compose up -d --force-recreate dsh dsh-forward`.

В Settings → Models → Add a custom provider:

| Поле | Значение |
|---|---|
| Provider ID | local-qwen |
| API protocol | openai-completions |
| Base URL | http://llama:8080/v1 |
| API key | local-only |
| Model ID | qwen3.8-27b |
| Context window | 131072 |
| Max output tokens | 8192 |

Выберите модель для новой сессии. `local-only` — непустое значение для клиента; аутентификация llama API в этом локальном стеке не включена. Другим машинам порты не опубликованы. Не вводите DeepSeek API key: для локальной Qwen он не нужен. Не используйте облачные search/title/summary-провайдеры, если хотите полностью локальную обработку; если в настройках есть вспомогательные модели, назначьте им local-qwen.

Если адаптер жалуется на формат запроса, в существующую запись provider в `$DSH_HOME/settings.yaml` добавьте compat.supportsDeveloperRole: false и compat.maxTokensField: max_tokens. Не перезаписывайте весь файл. Документированный редактор: Settings → Open configuration file; внутри Docker файл находится в /home/node/.dsh/settings.yaml. Не передавайте `deepseek`-специфичные reasoning поля модели Qwen без проверки.

Контекст 131072 включает инструкции, tools, историю, результаты команд, reasoning и ответ. При max output 8192 держите вход примерно до 115–120K, оставляя запас служебным сообщениям. Настройте compaction до предела; в ходе первых длинных сессий проверьте, что выбранная версия DSH учитывает локальный context window. Начинайте с одной задачи; параллельные агентские вызовы попадут в очередь одного слота.

Папка ./workspace — рабочие файлы агента. UID node внутри контейнера — 1000; папка должна быть доступна ему на запись. При другом UID WSL исправьте права именно этой папки. Компиляторы/тесты, запущенные агентом, могут потреблять много CPU/RAM отдельно от инференса.

7. Grafana и проверка OpenTelemetry

Откройте http://127.0.0.1:3000, пользователь admin. Пароль — значение GRAFANA_PASSWORD в .env. Источники Prometheus и Tempo и dashboard Local Qwen inference provisioned автоматически.

Схема: DSH + LoongSuite → OTLP/HTTP → Collector → Tempo (traces), Collector → Prometheus (metrics); llama /metrics и gpu-exporter → Prometheus; Grafana читает оба хранилища. Стандартный session-telemetry-otel DSH не заменяет GenAI tracing-плагин.

После запроса из DSH подождите 60–90 секунд:

```bash
curl --fail http://127.0.0.1:8889/metrics | grep gen_ai
curl --fail http://127.0.0.1:8080/metrics
curl --fail http://127.0.0.1:9835/metrics
bash ./docker-wsl.sh compose logs --tail=100 otel tempo dsh
```

В Prometheus http://127.0.0.1:9090/targets все три scrape targets должны быть UP. В Grafana Explore → Tempo найдите service.name=dsh-agent, разверните вызовы LLM/tool и проверьте gen_ai.usage.input_tokens/output_tokens. Плагин экспортирует также gen_ai.client.token.usage и gen_ai.client.operation.duration. Их Prometheus-имена нормализуются Collector; смотрите фактический /metrics, а не подставляйте непроверенное имя в dashboard.

Встроенный dashboard показывает вычисленные llama токены, скорость, очередь и доступность. Токены, обработанные движком, могут отличаться от полного usage запроса из-за повторного использования префикса. Для расхода по агентским вызовам используйте GenAI usage из DSH; не суммируйте обе системы. Не прибавляйте reasoning повторно к completion, если это его подмножество. Для histogram token usage суммируются _sum, не _count. В стандартном профиле тексты промптов не записываются в OTel spans.

В Grafana импортируйте официальный GPU dashboard 14574 либо 25547, выбрав Prometheus. Сверьте UUID обеих карт. Некоторые поля WSL могут отсутствовать или быть N/A; это не нулевая нагрузка.

Если WSL не дает VRAM/utilization/power, используйте exporter в Windows как единственное исключение из Docker. Остановите контейнер `bash ./docker-wsl.sh compose stop gpu-exporter`. В PowerShell администратора:

```powershell
winget install --scope machine utkuozdemir.nvidia_gpu_exporter
nvidia_gpu_exporter install
Start-Service nvidia_gpu_exporter
```

В prometheus.yaml замените gpu-exporter:9835 на host.docker.internal:9835 и выполните `bash ./docker-wsl.sh compose restart prometheus`. Проверьте доступ из контейнерной сети. При необходимости разрешите входящий TCP 9835 в Windows Firewall только от сети Docker/WSL. Чтобы обычный `compose up -d` не запускал старый контейнер, добавьте ему `profiles: [wsl-gpu]` в Compose. Если и Windows не отдает отдельный показатель, оставьте его недоступным; мониторинг не создает отсутствующую метрику.

8. Приемка длинного контекста

Сначала короткий smoke test, затем реальные задачи с входом около 32K, 64K, 96K и 115–120K токенов. Измеряйте токены через tokenizer/usage сервера, не количеством символов. Проверяйте длинный prefill и несколько тысяч output tokens, затем повторный ход и tool call. Проведите 30–60 минут под типичной нагрузкой и с обычными Windows-приложениями.

При OOM во время prefill сначала QWEN_UBATCH_SIZE=64. Если тесно всегда — QWEN_CTX_SIZE=65536 и такой же лимит контекста в DSH. Если переполняется только одна карта — корректируйте split. Изменения .env применяются через `bash ./docker-wsl.sh compose up -d --force-recreate llama`. Не компенсируйте OOM уменьшением offload слоев: это нарушит вашу цель. Для 128K уже выбран UD-Q4_K_M; лимит контекста в DSH должен быть 131072. Q5/128K и Q6/64K — эксперимент после замеров, не гарантированный режим. Не задавайте native 262K автоматически.

RAM cache и context checkpoints отключены для ограничения памяти; в агентских диалогах это может ухудшать повторное использование истории и увеличивать prefill. Увеличивать checkpoints следует только с отдельными замерами памяти. Две карты не обещают двукратного ускорения: layer split выбран для умеренного межкарточного обмена; PCIe topology и WSL влияют на скорость.

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

Сохраняйте .env, конфиги, models/revision.txt, models/SHA256SUMS, workspace и backup named volumes (для согласованной копии остановите соответствующие сервисы). Не используйте `bash ./docker-wsl.sh compose down -v`: это удалит историю/настройки. После обновления DSH/плагина повторяйте tool/usage/trace тесты.

Источники, проверенные 16 сентября 2026:

- Архитектура: https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/config.json
- GGUF и размеры: https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/tree/main
- llama flags и metrics: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Размещение input embedding: https://github.com/ggml-org/llama.cpp/blob/master/src/llama-model.cpp
- Docker CUDA: https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md
- CUDA WSL ограничения: https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- Docker Desktop GPU: https://docs.docker.com/desktop/features/gpu/
- DSH local providers: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md
- DSH Web: https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/web-app/README.md
- OTel plugin: https://github.com/loongsuite/dsh-plugin
- Collector: https://opentelemetry.io/docs/collector/configuration/
- Tempo: https://grafana.com/docs/tempo/latest/configuration/
- GPU exporter: https://github.com/utkuozdemir/nvidia_gpu_exporter
