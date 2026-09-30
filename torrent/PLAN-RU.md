# План: скачивание торрентов из Telegram-бота

## Задача

Администратор отправляет в существующего TG-бота magnet-ссылку. Бот показывает
метаданные торрента (название, размер) отдельным сообщением и просит
подтверждение. После подтверждения скачивание запускается в директорию
DESTINATION. Функциональность доступна только администратору. По окончании —
новый дашборд Grafana: общий объем скачанных/отданных данных, количество
загрузок, средняя скорость по каждой загрузке и медианная по всем, свободное
место на диске в директории, плюс дополнительные метрики.

## Решение

Новый сервис `torrent` (субпроект в этой директории) на Python 3.12 + FastAPI +
libtorrent (официальные биндинги, PyPI-пакет `libtorrent` 2.1.1, manylinux
wheel). Один контейнер, без GPU. tgbot получает команду `/magnet` и HTTP-клиент
к сервису. Мониторинг: Prometheus-джоб, алерт, дашборд `local-torrent`.

Проверено на связке Python 3.12 / libtorrent 2.1.1 (офлайн-сценарий с локальным
сидером): получение метаданных, скачивание, завершение, сидирование.

## Сервис `torrent`

Порт: `127.0.0.1:8087` -> 8080 контейнера. Данные: `./outputs/torrent:/data`,
скачивания в `TORRENT_DESTINATION` (по умолчанию `/data/downloads`).
Авторизация: `Bearer TGBOT_ADMIN_KEY` (один ключ на весь стек).

API (ключ обязателен, кроме health/metrics):

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health/live`, `/health/ready`, `/metrics` | без ключа |
| POST | `/api/metadata` `{"magnet": ...}` | метаданные: `info_hash, name, size, num_files, files` |
| POST | `/api/torrents` `{"magnet": ...}` | запустить скачивание в DESTINATION |
| GET | `/api/torrents` | список: `info_hash, name, state, progress, download_speed, upload_speed, size` |
| DELETE | `/api/torrents/{info_hash}` | убрать из очереди (файлы на диске остаются) |

Движок (libtorrent 2.1.1, особенности версии учтены):

- метаданные: торрент добавляется с флагами `paused | default_dont_download`
  (в 2.1 один только `paused` уже скачивает данные). Ждём `has_metadata()`
  до `TORRENT_METADATA_TIMEOUT` (по умолчанию 180 с), затем отдаём
  название/размер/файлы. Торрент остаётся в сессии.
- запуск: `prioritize_files([1]*n)` + `resume()` (+ `force_reannounce()`).
- ключ записи в реестре — hex-хэш из самой magnet-ссылки: `xt=urn:btih:`
  (40 hex) либо `xt=urn:btmh:` (64 hex), btih в приоритете. В 2.1
  `add_torrent_params.info_hash` у v2-торрентов может отличаться от btih,
  поэтому ключ не берем из биндинга.
- фоновый цикл (5 с): скорости по торрентам, суммарные счетчики payload
  (диффы `total_payload_download/upload`, монотонные — при удалении торрента
  не уменьшаем), завершение/ошибки, свободное место (`shutil.disk_usage`).
- завершение: `state in (finished, seeding)` для торрента в состоянии
  «скачивается» (у dont_download-торрента `finished` сразу после метаданных —
  на него это правило не распространяется, состояние отслеживает реестр).
  Записываем гистограммы (средняя скорость = payload/длительность, длительность,
  размер) и шлем уведомление админам через `tgbot /api/notify`
  (тот же паттерн, что у scraper).
- DHT: стандартные роутеры + дефолтный `dht_bootstrap_nodes`.

Метрики (job `torrent`):

| Метрика | Тип | Назначение |
|---|---|---|
| `torrent_downloaded_bytes_total` | Counter | скачанный payload (суммарно) |
| `torrent_uploaded_bytes_total` | Counter | отданный payload (сидирование, суммарно) |
| `torrent_downloads_total{result}` | Counter | started / completed / failed / cancelled |
| `torrent_download_speed_bytes_per_second{info_hash}` | Gauge | текущая скорость загрузки по каждому торренту |
| `torrent_upload_speed_bytes_per_second{info_hash}` | Gauge | текущая скорость отдачи по каждому торренту |
| `torrent_download_avg_speed_bytes_per_second` | Histogram | одна наблюдения на завершившуюся загрузку: её средняя скорость |
| `torrent_download_duration_seconds` | Histogram | длительность загрузок |
| `torrent_download_size_bytes` | Histogram | размеры загрузок |
| `torrent_active_downloads` | Gauge | скачивается прямо сейчас |
| `torrent_seeding` | Gauge | завершившиеся, сидируют |
| `torrent_metadata_fetches_total{result}` | Counter | ok / timeout / error / duplicate |
| `torrent_destination_free_bytes` | Gauge | свободно в DESTINATION |
| `torrent_destination_total_bytes` | Gauge | размер ФС DESTINATION |

«Средняя скорость по каждой загрузке» — наблюдения гистограммы
`torrent_download_avg_speed_bytes_per_second` (средняя = sum/count за период,
по каждой загрузке — таблица по `info_hash` на основе скоростей).
«Медианная по всем загрузкам» — `histogram_quantile(0.5, ...)`.

## Изменения в tgbot

- `torrents.py` — httpx-клиент к сервису (`metadata`, `start`, `list`,
  `remove`), валидатор magnet-ссылки (btih/btmh), человекочитаемые размеры.
  Сервис выключен, если `TGBOT_TORRENT_URL` не задан (команды отвечают
  «сервис не настроен»).
- `chat.py`:
  - `/magnet <ссылка>` (только админ): проверка ссылки -> «Получаю
    метаданные…» -> сообщение с названием, размером, числом файлов и
    кнопками [Скачать] [Отмена]. `/cancel` работает, пока идут метаданные.
    Ожидающие подтверждения хранятся в памяти (TTL 15 минут).
  - callback `magnet-yes:<hash>` -> запуск скачивания; `magnet-no:<hash>` ->
    отказ. Оба — только для администратора.
  - `/torrents` (только админ) — список загрузок: состояние, прогресс, скорость.
  - `/torrent-del <hash>` (только админ) — убрать из очереди.
- `bot.py` — роутеры новых команд; `Out.edit` умеет обновлять inline-кнопки.
- `app.py`, `settings.py` — клиент и `TGBOT_TORRENT_URL`.
- Тесты: фейковый клиент; отказ не-админу, метаданные -> кнопки, подтверждение
  -> старт, отмена, `/torrents`, `/torrent-del`, истечение ожидания.

## Инфраструктура

- `compose.yaml`: сервис `torrent` (healthcheck, volume, порт 8087) и
  `TGBOT_TORRENT_URL=http://torrent:8080` у tgbot.
- `prometheus.yaml`: job `torrent`.
- `alerts.yaml`: job `torrent` в исключение `ScrapeTargetDown`; новый алерт
  `TorrentDestinationLow` (свободно < 5%).
- `Makefile`: `torrent`, `torrent-build`, `torrent-test`, `logs-torrent` + help.
- `README-RU.md`, `TGBOT-RU.md`: описание.

## Дашборд `local-torrent` (Grafana)

- Stat: скачано за период, отдано за период, загрузок за период (по result),
  свободно в DESTINATION.
- Time series: текущие скорости загрузки/отдачи по торрентам (`info_hash`);
  средняя скорость загрузок за период; медианная скорость
  (`histogram_quantile(0.5, ...)`); активные загрузки/сидирование;
  свободное место.
- Текстовый блок «Как читать показатели».

## Тесты

- `torrent/tests`: валидация magnet; движок офлайн (локальный сидер:
  метаданные -> подтверждение -> скачивание -> завершение -> сидирование,
  счетчики, гистограммы, отмена, дубликат); API: ключ, 409, список, удаление,
  health, метрики.
- `tgbot/tests`: сценарии команд с фейковым клиентом.
- Команды: `make torrent-test`, `make tgbot-test` (в контейнере, без сети).

## Этапы

1. План (этот документ).
2. Сервис `torrent`: код, Dockerfile, requirements, тесты.
3. tgbot: клиент, команды, тесты, документация.
4. Инфраструктура: compose, prometheus, alerts, Makefile, README.
5. Дашборд Grafana.
6. Сборка, тесты; сквозная проверка с настоящим magnet — вручную (нужна сеть).
