# PI WEB

[PI WEB](https://pi-web.dev) — веб-интерфейс для [Pi Coding Agent](https://pi.dev).
Агент работает с файлами, shell, git и Docker. В качестве моделей используются
локальные llama-server из этого стека.

Сервис состоит из двух контейнеров на одном образе `local/ml-piweb:1`:

- `piweb-sessiond` — демон, в котором идут сессии агента. Сессии не прерываются,
  если закрыть браузер или перезапустить `piweb`.
- `piweb` — веб-интерфейс и API, http://127.0.0.1:8504.

Сервис не занимает GPU, поэтому `make qwen`, `make bonsai-mtp` и `make models-stop`
его не останавливают. Модель запускается отдельно.

## Запуск

```bash
make piweb-build   # образ: Node 22, PI WEB, Pi Coding Agent, git, build-essential, docker CLI
make bonsai-mtp    # или make qwen / make qwen-mtp
make piweb         # http://127.0.0.1:8504
```

Если сборка падает с ошибкой `docker-credential-desktop.exe: exec format error`
(Docker Desktop в WSL), соберите образ с анонимным конфигом Docker:

```bash
d=$(mktemp -d) && python3 docker-public-config.py ~/.docker "$d" && DOCKER_CONFIG="$d" make piweb-build; rm -rf "$d"
```

Остальные команды: `make logs-piweb`, `make piweb-stop`. Остановка `piweb-sessiond`
прерывает активные сессии агента.

## Модели

Модели описаны в `piweb/models.json`: провайдер `local`, по модели на каждый
llama-server.

| Модель | Сервис | Запуск | Контекст |
| --- | --- | --- | --- |
| `qwen3.8-27b` | `qwen:8080` | `make qwen` | 196608 |
| `qwen3.8-27b-mtp` | `qwen-mtp:8080` | `make qwen-mtp` | 163840 |
| `bonsai2-27b-uc-mtp` | `bonsai-mtp:8080` | `make bonsai-mtp` | 262144 |

Одновременно работает только одна LLM. Поэтому в интерфейсе выбирайте модель,
которая сейчас запущена. Запрос к остановленной модели вернёт ошибку соединения.
Модель по умолчанию для новых сессий задаётся звёздочкой в селекторе модели.
Thinking включается уровнем `medium` и выключается уровнем `off`. Значение
передаётся в шаблон чата как `enable_thinking`.

Файл копируется в `outputs/piweb/data/pi-agent/models.json` при каждом старте
контейнера. Править его нужно в репозитории, затем перезапустить `piweb-sessiond`:
провайдеры читаются при старте демона. Если меняете `QWEN_CTX_SIZE` и похожие
переменные, обновите `contextWindow`. У `qwen` один слот (`--parallel 1`), поэтому
параллельные сессии ждут друг друга. У bonsai-mtp два слота с общим контекстом.

## Каталоги

| На хосте | В контейнере | Назначение |
| --- | --- | --- |
| `outputs/piweb/data` | `/data` | `HOME`, конфиг PI WEB, состояние Pi (сессии, настройки, auth) |
| `PIWEB_WORKSPACE`, по умолчанию `outputs/piweb/workspace` | `/workspace` | песочница для новых проектов |
| `PIWEB_PROJECTS`, по умолчанию `~/git` | тот же путь | внешние проекты |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker хоста |

Проекты (workspace) добавляются в интерфейсе: укажите `/workspace/<имя>` или путь
внутри `PIWEB_PROJECTS`, например `/home/ilya/git/zolak/zolak-studio`.

`PIWEB_PROJECTS` монтируется по тому же пути, что и на хосте. Поэтому `docker compose`
проектов, запущенный агентом через сокет, правильно разрешает относительные
bind mounts. Для `/workspace` это не работает: путь на хосте другой.

Конфиг PI WEB (`outputs/piweb/data/config/pi-web/config.json`) создаётся при первом
старте и дальше редактируется в Settings. В `pathAccess.allowedPaths` по умолчанию
входят `/workspace` и `PIWEB_PROJECTS`. После смены `PIWEB_PROJECTS` удалите этот
файл или исправьте его, затем выполните `make piweb`.

Переменные в `.env`:

```bash
PIWEB_PROJECTS=/home/ilya/git      # внешние проекты
PIWEB_WORKSPACE=/home/ilya/sandbox # другой каталог для /workspace
PIWEB_UID=1000                     # по умолчанию SDXL_UID / 1000
PIWEB_GID=1000
```

`PIWEB_DOCKER_GID` (группа сокета Docker) Makefile определяет сам через `stat`.
Если запускаете `docker compose` напрямую, задайте её в `.env`.

Git и SSH: `HOME` агента — `outputs/piweb/data/home`. Чтобы агент мог коммитить и
пушить, положите туда `.gitconfig` и при необходимости `.ssh/`.

## Безопасность

- PI WEB не изолирует агента и не имеет авторизации. Порт привязан только к
  `127.0.0.1`. Удалённый доступ — через SSH-туннель:
  `ssh -L 8504:127.0.0.1:8504 <host>`.
- Сокет Docker даёт права root на хосте: агент может запустить любой контейнер с
  любыми mount.
- У агента есть shell и выход в интернет. Всё, что смонтировано (в том числе
  конфиденциальные проекты в `~/git`), он теоретически может отправить наружу.
  Модели локальные, телеметрия Pi выключена (`PI_TELEMETRY=0`). Внешний сетевой
  доступ у агента остаётся.
