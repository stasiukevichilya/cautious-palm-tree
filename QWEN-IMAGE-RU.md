# Qwen-Image 2.1 на двух GPU

Полный генератор: DiT + Qwen3-VL Heretic + VAE. Это отдельный сервис
`qwen-image` на ComfyUI, а не модель чата в llama.cpp. Существующие сервисы
`qwen` и `gemma` остаются на llama.cpp без изменения их моделей.

## Подготовка и запуск

Требуются уже настроенные Docker Compose, NVIDIA GPU в Docker, Python 3.11+
и curl. Новые Python-пакеты устанавливаются только внутри образа.

```bash
make qwen-image-build
make qwen-image-download
python3 -m unittest discover -s qwen-image/tests -v
make qwen-image
make qwen-image-test
make qwen-image-browser-test
```

`make qwen-image-browser-test` открывает UI в headless Chromium (образ
Playwright из SDXL), загружает workflow и сохраняет скриншоты в
`outputs/qwen-image/screenshots`.

Интерфейс и API: http://127.0.0.1:8083. В списке сохраненных workflows
доступен `qwen-image21.json`. Его исходник:
`qwen-image/workflows/qwen-image21.ui.json`; можно также открыть этот файл
через меню ComfyUI. Измените prompt и запустите очередь. Seed в UI
по умолчанию `randomize`: при неизменном графе ComfyUI возвращает результат
из кэша без генерации. При первом старте в `outputs/qwen-image/user` создаются
копия workflow и настройки без окна шаблонов; существующие файлы не
перезаписываются. Чтобы получить обновленный workflow после изменения образа,
удалите `outputs/qwen-image/user/default/workflows/qwen-image21.json`.

Встроенный стартовый граф фронтенда ComfyUI рассчитан на Z-Image-Turbo, модели
которого здесь не установлены. Локальное расширение
`qwen-image/local_nodes/web/default_workflow.js` заменяет его на
`qwen-image21.json`, а новые вкладки открываются пустыми
(`Comfy.NewBlankWorkflow`). Шаблоны из меню Templates требуют других моделей
и в этом профиле не работают.

`make qwen-image` останавливает SDXL и LLM перед запуском. Обратное переключение
через `make qwen`, `make gemma`, `make sdxl` или `make sdxl-dual` останавливает
Qwen-Image. `make models-stop` освобождает GPU от всех моделей проекта.
Прямой `docker compose up` не выполняет взаимное исключение моделей.

Triton (через comfy-kitchen) компилирует CUDA-помощник при старте, поэтому в
образе есть gcc. Контейнерные runtime NVIDIA, в том числе WSL, монтируют только
`libcuda.so.1`; `launch.py` находит его через `ldconfig` и передает Triton
симлинк `libcuda.so` через `TRITON_LIBCUDA_PATH`.

Веса занимают около 14.7 GB и лежат в `models/qwen-image-2.1`.
Загрузчик фиксирует revision и SHA-256 всех четырех файлов в
`qwen-image/models.json`, поддерживает продолжение `.part` и не перезаписывает
неожиданные существующие файлы. При испорченном `.part` переместите его отдельно
и повторите загрузку. Работающий контейнер использует локальные веса без HF-сети.

## Размещение и ограничения

| Компонент | Формат | Карта |
|---|---|---|
| Qwen-Image 2.1 DiT | GGUF Q6_K, около 6.0 GB | 4070 Ti Super, CUDA 0 |
| Qwen3-VL-8B Heretic | GGUF Q6_K, около 6.9 GB | 3080 Ti, CUDA 1 |
| Vision projector | F16, около 1.2 GB | 3080 Ti, CUDA 1 |
| VAE | BF16, около 0.7 GB | 4070 Ti Super, CUDA 0 |

GPU выбираются переменными `QWEN_IMAGE_GPU0` и `QWEN_IMAGE_GPU1` в `.env`.
По умолчанию используются уже подготовленные `SDXL_GPU0` / `SDXL_GPU1`.
Предпочтительны UUID из `nvidia-smi -L`, а не меняющиеся числовые индексы.
Порядок важен: первой должна быть 16-GB карта. UID/GID наследуются от
`SDXL_UID` / `SDXL_GID`; переопределения: `QWEN_IMAGE_UID` / `QWEN_IMAGE_GID`.

Профиль требует ровно две видимые CUDA GPU. CPU-offload, dynamic VRAM,
async offload и pinned memory отключены. Локальные loaders проверяют устройства
всех параметров и buffers после полной загрузки. KV-кэш DiT явно установлен
в `gpu`, не в `auto`, который допускает RAM. VAE декодируется плитками 512.

Предел аллокатора PyTorch для каждой карты равен свободной памяти на старте
минус `QWEN_IMAGE_VRAM_RESERVE_GIB` (по умолчанию 1.5 GiB). Если памяти
недостаточно, запрос должен завершиться ошибкой, а не перейти на CPU.
Это не запрет системному драйверу WDDM использовать shared memory: оставляйте
GPU свободными от других приложений и проверяйте фактическое потребление.

Полностью исключить CPU/RAM нельзя: Python, токенизация, чтение/десериализация
весов, HTTP, PNG и интерфейс используют хост. Требование GPU-only относится
к нейросетевым вычислениям и резидентным весам, а не к нулевому RSS процесса.

Стартовый workflow: text-to-image, 1024x1024, batch 1, 25 шагов, CFG 1,
Euler/simple. `resolution` в `TextEncodeQwenImage21` задает квадрат; для
другого соотношения сторон подключите к `latent_image` узла KSampler узел
`EmptySD3LatentImage` (стороны кратны 16), см. `smoke.py --width/--height`.

Проверенные разрешения (seed 7, 25 шагов, прогретые веса; пик VRAM по
`nvidia-smi`, включая прочие процессы Windows; CUDA 1 с энкодером ~9.1 GiB
не зависит от разрешения):

| Размер | Время | с/шаг | Пик CUDA 0 |
|---|---|---|---|
| 1024x1024 | ~42 с | 1.4 | 10.7 GiB |
| 1280x720 (16:9) | 38 с | — | — |
| 1536x1536 | 84 с | — | 11.4 GiB |
| 2048x1152 (16:9) | 88 с | 3.0 | 11.3 GiB |
| 2048x2048 | 164 с | — | 12.5 GiB |
| 2560x1440 (16:9, QHD) | 140 с | 4.9 | 12.3 GiB |
| 2304x2304 | 216 с | — | 13.3 GiB |

2560x2560 не завершался (тест прерван; 10.6 с/шаг, OOM не было). Длинные
prompts, image editing и сторонние workflows не являются проверенным
профилем по памяти.

```bash
docker compose --profile qwen-image exec -T qwen-image \
  python /opt/qwen-image/smoke.py --width 2560 --height 1440 --timeout 1800
``` UI позволяет
менять граф; не заменяйте локальные GPU-loaders и не переключайте KV-кэш на CPU.

## Вариант DiT: unsloth Q8_0

Вместо DiT Q6_K от pottokao можно использовать
[unsloth/Qwen-Image-2.1-GGUF](https://huggingface.co/unsloth/Qwen-Image-2.1-GGUF)
`qwen-image-2.1-Q8_0.gguf` (Unsloth Dynamic 2.0, 7.6 GB). Энкодер Heretic и VAE
общие, раскладка по GPU та же.

```bash
make qwen-image-download-unsloth   # +7.6 GB, SHA-256 закреплен в models.json
make qwen-image-unsloth            # пересоздает контейнер с QWEN_IMAGE_DIT=unsloth-q8_0
make qwen-image-test               # smoke берет workflow запущенного DiT
make qwen-image                    # обратно на Q6_K
```

Процесс обслуживает ровно один DiT, выбранный `QWEN_IMAGE_DIT` при старте
(`base` или `unsloth-q8_0`). С `--gpu-only` ComfyUI не вытесняет замененный
DiT из VRAM: смена модели в том же процессе дает OOM, поэтому она требует
пересоздания контейнера. `/local-qwen-image/config` сообщает активный DiT и
workflow; UI открывает `qwen-image21-unsloth-q8.json`, workflow с другим DiT
отклоняется с HTTP 400.

GGUF от unsloth сделаны для stable-diffusion.cpp: без `general.architecture`
и с префиксом `model.diffusion_model.`. ComfyUI-GGUF грузит их в режиме
совместимости `sd.cpp`, но его закрепленная ревизия не знает Qwen-Image 2.1,
поэтому `local_nodes` регистрирует детектор архитектуры `qwen_image`.
Ключи и формы тензоров совпадают с Q6_K от pottokao. Предупреждение в логе
`loaded in compatibility mode 'sd.cpp' [arch:qwen_image]` ожидаемо.

Проверка без CPU-offload на 4070 Ti Super (16 GB) + 3080 Ti (12 GB),
seed 7, 25 шагов, прогретые веса. Пик — `max_memory_reserved` PyTorch на
CUDA 0 при бюджете аллокатора 13.23 GiB; CUDA 1 (энкодер) — 8.8 GiB
на всех размерах. Все модели проверены на `cuda:0`/`cuda:1`:

| Размер | Время | Пик CUDA 0 |
|---|---|---|
| 1024x1024 | ~34 с (1.2 с/шаг) | 9.4 GiB |
| 1536x1536 | 76 с | 10.4 GiB |
| 2048x1152 | 78 с | 10.4 GiB |
| 2048x2048 | 156 с | 11.0 GiB |
| 2560x1440 | 132 с | 11.0 GiB |
| 2304x2304 | 206 с | 11.8 GiB |

Q8_0 немного быстрее Q6_K (деквантование Q8_0 дешевле). F16 (14.2 GB) на
16-GB карту вместе с VAE и активациями не помещается; более мелкие кванты
unsloth помещаются, но не добавлены в манифест.

## HTTP API

Используется нативный API ComfyUI, не OpenAI images API:

- `POST /prompt`: JSON `{"prompt": <API graph>, "client_id": "my-client"}`.
- `GET /history/{prompt_id}`: статус и имена сохраненных изображений.
- `GET /view?filename=...&type=output&subfolder=...`: готовый PNG.
- `GET /queue`, `POST /interrupt`: очередь и прерывание текущей генерации.
- `GET /system_stats`: доступность сервера; не означает прогрев всех весов.
- `GET /local-qwen-image/gpu`: устройства загруженных моделей и CUDA-пики.
- `GET /local-qwen-image/metrics`: метрики Prometheus (готовность, очередь,
  завершённые промпты с длительностью, свободная VRAM); их скрейпит Prometheus
  (job `qwen-image`).

Готовый API graph: `qwen-image/workflows/qwen-image21.api.json`.
Пример клиента с ожиданием результата: `qwen-image/smoke.py`
(`--seed` задает базовый seed, по умолчанию случайный).
Тест проверяет декодируемый непустой PNG 1024x1024, размещение трех моделей
на заданных CUDA и минимум 1 GiB свободной VRAM после генерации.
Ориентиры на 4070 Ti Super + 3080 Ti: первый запрос с загрузкой весов около
90 с, далее около 42 с на изображение (25 шагов); после генерации свободно
около 8.2 GiB на CUDA 0 и 2.5 GiB на CUDA 1.

```bash
docker compose --profile qwen-image exec -T qwen-image \
  python /opt/qwen-image/smoke.py --count 10
make logs-qwen-image
curl --fail http://127.0.0.1:8083/local-qwen-image/gpu
```

PNG: `outputs/qwen-image/output`. Отчеты: подкаталог `benchmarks`.
Входные изображения, workflows и настройки: `outputs/qwen-image/input` и
`outputs/qwen-image/user`. Автоматической очистки PNG нет. Каталог смонтирован
с правами пользователя; не запускайте подготовку через sudo.
Логи входят в общий Grafana dashboard моделей (модель `qwen-image`). Prometheus
собирает `/local-qwen-image/metrics` (job `qwen-image`): готовность, очередь,
длительность генераций и VRAM сервиса; собственный dashboard —
http://127.0.0.1:3055/d/local-qwen-image. GPU-метрики всей машины собирает
существующий NVIDIA exporter. Остановленный профиль не вызывает
ScrapeTargetDown; после перезапуска контейнера счётчики метрик сбрасываются.

Сервис доступен только через loopback хоста. В ComfyUI нет настроенной
аутентификации, поэтому не публикуйте его напрямую в Интернет.

## Источники и лицензии

- [Qwen-Image 2.1](https://huggingface.co/Qwen/Qwen-Image-2.1): полный генератор;
  лицензия Qwen Research, с ограничением коммерческого использования.
- [Запрошенный Heretic encoder](https://huggingface.co/pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-GGUF):
  текстовый/визуальный энкодер, не самостоятельный генератор; Apache 2.0.
- [DiT GGUF](https://huggingface.co/pottokao/Qwen-Image-2.1-DiT-GGUF).
- [DiT GGUF unsloth](https://huggingface.co/unsloth/Qwen-Image-2.1-GGUF): лицензия Qwen Research.
- [Официальный workflow ComfyUI](https://docs.comfy.org/tutorials/image/qwen/qwen-image-2-1).

Ревизии ComfyUI, ComfyUI-GGUF и совместимого патча Qwen3-VL закреплены в
`qwen-image/fetch_sources.py`. Образ сохраняет список установленных пакетов
в `/opt/qwen-image-installed.txt`; разрешенные версии зафиксированы в
`qwen-image/requirements.lock`. Обновляйте компоненты совместно и повторяйте
GPU-тесты: поддержка Qwen-Image 2.1 зависит от новых узлов ComfyUI.
