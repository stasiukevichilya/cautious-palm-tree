# Applications (included from ../Makefile; recipes run from the repository root).
.PHONY: tgbot tgbot-build tgbot-key tgbot-test tgbot-browser-test logs-tgbot
.PHONY: scraper scraper-build scraper-test logs-scraper
.PHONY: torrent torrent-build torrent-test logs-torrent

help-services:
	@echo "== Сервисы (services/), работают при любой модели"
	@echo "make tgbot       - Telegram bot for the running LLM / image model, settings UI :8084"
	@echo "make tgbot-key / tgbot-build / tgbot-test / tgbot-browser-test / logs-tgbot"
	@echo "make scraper     - мониторинг БУ-объявлений (Kufar, Onliner), алерты в бота, API :8086"
	@echo "make scraper-build / scraper-test / logs-scraper"
	@echo "make torrent     - скачивание по magnet из бота (/magnet), API :8087"
	@echo "make torrent-build / torrent-test / logs-torrent"

tgbot-build:
	$(COMPOSE) build tgbot

# The bot is not a GPU model: model switching and models-stop leave it running.
tgbot: infra
	@grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env || { echo "Add TGBOT_ADMIN_KEY to .env: make tgbot-key"; exit 1; }
	mkdir -p outputs/tgbot
	$(COMPOSE) up -d --no-deps tgbot
	@echo "Telegram bot settings: http://127.0.0.1:8084"

tgbot-key:
	@grep -q '^TGBOT_ADMIN_KEY=' .env && echo "TGBOT_ADMIN_KEY already set in .env" || \
	  { printf 'TGBOT_ADMIN_KEY=%s\n' "$$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env; \
	    echo "TGBOT_ADMIN_KEY added to .env"; }

tgbot-test:
	docker run --rm --network none --tmpfs /tmp -e TGBOT_WORKFLOWS=/workflows \
	  -v $(CURDIR)/imagegen/qwen-image/workflows:/workflows:ro local/ml-tgbot:1 python -m unittest discover -s tests -v

tgbot-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f imagegen/sdxl/tests/Dockerfile.browser imagegen/sdxl/tests
	mkdir -p outputs/tgbot/screenshots
	TGBOT_ADMIN_KEY=$$(sed -n 's/^TGBOT_ADMIN_KEY=//p' .env) \
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_default -e TGBOT_ADMIN_KEY \
	  -v $(CURDIR)/services/tgbot/tests/browser.cjs:/test/browser.cjs:ro \
	  -v $(CURDIR)/outputs/tgbot/screenshots:/screenshots \
	  local/sdxl-browser-test:1

logs-tgbot:
	$(COMPOSE) logs --tail=100 -f tgbot

scraper-build:
	$(COMPOSE) build scraper

# Как и бот, не модель: переключение моделей и models-stop оставляют его работать.
scraper: infra
	@grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env || { echo "Add TGBOT_ADMIN_KEY to .env: make tgbot-key"; exit 1; }
	mkdir -p outputs/scraper
	$(COMPOSE) up -d --no-deps scraper
	@echo "Scraper API: http://127.0.0.1:8086 (Bearer TGBOT_ADMIN_KEY)"

scraper-test:
	docker run --rm --network none --tmpfs /tmp local/ml-scraper:1 python -m unittest discover -s tests -v

logs-scraper:
	$(COMPOSE) logs --tail=100 -f scraper

torrent-build:
	$(COMPOSE) build torrent

# Как и бот, не модель: переключение моделей и models-stop оставляют его работать.
torrent: infra
	@grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env || { echo "Add TGBOT_ADMIN_KEY to .env: make tgbot-key"; exit 1; }
	mkdir -p outputs/torrent
	$(COMPOSE) up -d --no-deps torrent
	@echo "Torrent API: http://127.0.0.1:8087 (Bearer TGBOT_ADMIN_KEY); в боте: /magnet <ссылка>"

# Engine tests use a real libtorrent session on loopback, so the container keeps its network.
torrent-test:
	docker run --rm --tmpfs /tmp local/ml-torrent:1 python -m unittest discover -s tests -v

logs-torrent:
	$(COMPOSE) logs --tail=100 -f torrent
