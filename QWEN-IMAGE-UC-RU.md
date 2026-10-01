# Qwen-Image 2.1 Uncensored

Отдельный сервис `qwen-image-uc` на ComfyUI для
[abenzerps/Qwen-Image-2.1-Uncensored-GGUF](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF).
Модель не содержит фильтра контента. Сервис запускается только вручную,
недоступен Telegram-боту и открыт только на loopback хоста.

## Подготовка и запуск

```bash
make qwen-image-uc-build
make qwen-image-uc-download     # 14.6 GB в models/qwen-image-2.1-uc
make qwen-image-uc              # останавливает остальные модели; UI и API http://127.0.0.1:8085
make qwen-image-uc-test
make qwen-image-uc-browser-test
```

`make qwen`, `make sdxl`, `make qwen-image` и `make models-stop`
останавливают этот сервис. Telegram-бот (`make tgbot`) продолжает работать, но
к UC-сервису доступа не имеет.

## Конфигурация по карточке модели

| Компонент | Файл | Где работает |
|---|---|---|
| DiT | `qwen-image-2.1-UC-Q4_K_M.gguf`, 4.6 GB (рекомендован карточкой) | GPU, VRAM |
| Текстовый энкодер | `qwen3vl_8b_int8_convrot.safetensors`, 9.35 GB (рекомендован для экономии памяти) | CPU, системная RAM |
| VAE | `qwen_image_2.1_vae_bf16.safetensors`, 0.68 GB | GPU, VRAM |

Карточка рекомендует держать DiT в VRAM, а энкодер — в RAM на CPU: он работает
один раз на промпт. Это единственный CPU-offload в профиле. DiT и VAE
загружаются целиком в VRAM и проверяются локальными loaders; энкодер
загружается через штатный `CLIPLoader` с `device=cpu` (`type=qwen_image`).
ComfyUI запускается с `--highvram` (DiT и VAE не выгружаются между запросами),
без `--gpu-only`: он поместил бы энкодер в VRAM. Dynamic VRAM, async offload и
pinned memory выключены, KV-кэш DiT — `gpu`.

Нужна одна GPU: по умолчанию первая карта профиля Qwen-Image
(`QWEN_IMAGE_GPU0`, иначе `SDXL_GPU0`); переопределение — `QWEN_IMAGE_UC_GPU`
в `.env`. Резерв VRAM — `QWEN_IMAGE_UC_VRAM_RESERVE_GIB` (1.5 GiB).

По указанию карточки используется форк
[leejet/ComfyUI-GGUF](https://github.com/leejet/ComfyUI-GGUF): файлы
сконвертированы stable-diffusion.cpp с архитектурой `qwen_image21`, которую
`city96/ComfyUI-GGUF` не знает (`Unexpected architecture type`). Форк — это
закрепленная в qwen-image ревизия city96 плюс 7 небольших коммитов;
ревизия закреплена в `qwen-image-uc/fetch_sources.py`. ComfyUI и
Python-зависимости те же, что у qwen-image (общие `constraints.txt` и
`requirements.lock`).

Workflow повторяет официальный шаблон Comfy-Org, на который ссылается
карточка, с заменой загрузчика на GGUF: 1024x1024 (`EmptyLatentImage`),
25 шагов, CFG 1, Euler/simple, `VAEDecode`. Шаблон указывает, что официальный
pipeline использует 40–50 шагов; negative prompt не действует при CFG 1.
Улучшатель промпта из шаблона (отдельная LLM 9B) не включен.

## Изоляция от Telegram-бота

- Бот обращается только к сервисам `qwen`, `sdxl`, `qwen-image` по
  имени сервиса: другие хосты, IP-адреса и `host.docker.internal` отклоняются
  API настроек и игнорируются при чтении из базы (`tgbot/settings.py`,
  `ALLOWED_BACKENDS`). Это основная гарантия.
- Сервис подключен только к своей docker-сети `qwen-image-uc`, поэтому имя
  `qwen-image-uc` из контейнера бота не разрешается. Единственное исключение —
  prometheus, подключённый к этой сети только для скрейпинга
  `/local-qwen-image-uc/metrics`; бот на ней отсутствует. На Docker Desktop это
  не полная изоляция: проверено, что из контейнера бота сервис доступен по IP
  чужой bridge-сети и через `host.docker.internal:8085`, а запросы с хоста и из
  контейнеров приходят с одного адреса шлюза, так что фильтр по IP невозможен.
- UC-сервис не отдает `/local-qwen-image/config`, без которого бот не строит
  граф ComfyUI.
- Все условия покрыты тестами (`qwen-image-uc/tests`, `tgbot/tests`).

## Проверенные результаты

RTX 4070 Ti Super (16 GB), CPU 16 потоков под WSL, seed 7, 25 шагов, веса
прогреты. Пик VRAM — `max_memory_reserved` PyTorch при бюджете 13.23 GiB и
`nvidia-smi` (включая прочие процессы Windows). Вторая GPU не используется.

| Размер | Время | Пик PyTorch | Пик nvidia-smi |
|---|---|---|---|
| 1024x1024, тот же промпт | 34 с (1.3 с/шаг) | 7.0 GiB | 8.4 GiB |
| 1024x1024, новый промпт | 70 с | 7.0 GiB | 8.4 GiB |
| 1536x1536 | 110 с | 10.2 GiB | 11.5 GiB |
| 2048x1152 | 76 с | 10.2 GiB | 12.1 GiB |
| 2048x2048 | 149 с | 12.6 GiB | 12.0 GiB |

Первый запрос после старта с загрузкой весов — около 90 с. Энкодер
занимает около 8.7 GiB RAM, пик RAM хоста в тесте — 10 GiB.

Кодирование нового промпта на CPU занимает около 36 с. Карточка обещает, что
энкодер на CPU почти не влияет на скорость; здесь это не так. ComfyUI кэширует
результат, поэтому повтор того же промпта с другим seed этих 36 с не тратит.
Если это критично, энкодер можно перенести на вторую GPU — это отступление от
рекомендованной конфигурации.

При 2048x2048 до предела бюджета остается около 0.6 GiB, потому что шаблон
использует обычный `VAEDecode`. При нехватке памяти ComfyUI сам повторяет
декодирование плитками на той же GPU. Большие размеры не проверялись.

## Данные

PNG: `outputs/qwen-image-uc/output`; workflows и настройки UI:
`outputs/qwen-image-uc/user`. Автоматической очистки нет. Логи идут в общий
dashboard Grafana (модель `qwen-image-uc`); тексты промптов ComfyUI в лог не
пишет. Prometheus собирает `/local-qwen-image-uc/metrics` (job
`qwen-image-uc`); собственный dashboard — http://127.0.0.1:3055/d/local-qwen-image-uc.
Аутентификации нет — не публикуйте порт наружу.

Лицензия: Qwen Research License, как у базовой модели.
