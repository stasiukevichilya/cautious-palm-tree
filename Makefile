.DEFAULT_GOAL := help

# Не допускаем одновременного выполнения целей через make -j.
.NOTPARALLEL:

COMPOSE := docker compose
INFRA := prometheus grafana tempo otel gpu-exporter loki alloy
MODEL_PROFILES := --profile qwen --profile qwen-mtp --profile bonsai-mtp --profile sdxl --profile qwen-image --profile qwen-image-uc --profile strata
SDXL_COMPOSE := $(COMPOSE) -f compose.yaml -f compose.sdxl-dual.yaml
# GID of the Docker socket, so the agent terminal (Open WebUI) can drive the host Docker.
export AGENT_DOCKER_GID ?= $(shell stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)
QWEN_IMAGE_DIT ?= base

.PHONY: help infra qwen qwen-mtp bonsai-mtp bonsai-mtp-build strata strata-build logs-strata sdxl sdxl-dual sdxl-build sdxl-download sdxl-test models-stop status logs-qwen logs-qwen-mtp logs-bonsai-mtp logs-sdxl stop down
.PHONY: uc-bonsai qwen-image-uc qwen-image-uc-build qwen-image-uc-download qwen-image-uc-test qwen-image-uc-browser-test logs-qwen-image-uc
.PHONY: tgbot tgbot-build tgbot-key tgbot-test tgbot-browser-test logs-tgbot
.PHONY: scraper scraper-build scraper-test logs-scraper
.PHONY: torrent torrent-build torrent-test logs-torrent
.PHONY: agent agent-key agent-build agent-skills agent-functions agent-stop logs-agent images-build images-test blender-addon
.PHONY: qwen-image qwen-image-build qwen-image-download qwen-image-download-unsloth qwen-image-unsloth qwen-image-test qwen-image-browser-test logs-qwen-image

help:
	@echo "make qwen-image-uc - Qwen-Image 2.1 Uncensored, one GPU + CPU text encoder, UI :8085 (not in the bot)"
	@echo "make qwen-image-uc-build / qwen-image-uc-download / qwen-image-uc-test / qwen-image-uc-browser-test"
	@echo "make uc-bonsai - qwen-image-uc (encoder on GPU1) + bonsai-mtp (24k ctx) together, no CPU offload"
	@echo "make tgbot       - Telegram bot for the running LLM / image model, settings UI :8084"
	@echo "make tgbot-key / tgbot-build / tgbot-test / tgbot-browser-test / logs-tgbot"
	@echo "make scraper     - мониторинг БУ-объявлений (Kufar, Onliner), алерты в бота, API :8086"
	@echo "make scraper-build / scraper-test / logs-scraper"
	@echo "make torrent     - скачивание по magnet из бота (/magnet), API :8087"
	@echo "make torrent-build / torrent-test / logs-torrent"
	@echo "make agent       - Open WebUI в локальной сети :1098 + терминал со скиллами + MCP images; модель поднимать отдельно"
	@echo "make agent-key / agent-build / agent-skills / agent-functions / agent-stop / logs-agent / images-test"
	@echo "make blender-addon - поставить аддон blender-mcp в Blender на Windows (BLENDER_VERSION=4.0)"
	@echo "make qwen-image  - Qwen-Image 2.1 on two GPUs, API/UI :8083"
	@echo "make qwen-image-build / qwen-image-download / qwen-image-test / qwen-image-browser-test"
	@echo "make qwen-image-unsloth - the same with unsloth Q8_0 DiT (make qwen-image-download-unsloth first)"
	@echo "make infra       — запустить инфраструктуру без моделей"
	@echo "make qwen        — запустить Qwen"
	@echo "make qwen-mtp    — Qwen с MTP-спекуляцией (RVN Q4_K_M multilingual mtp), порт :8081; взаимно исключается с make qwen"
	@echo "make bonsai-mtp  — Ternary-Bonsai-2-27B Uncensored PQ2_0 + MTP (форк PrismML llama.cpp), порт :8089"
	@echo "make bonsai-mtp-build — собрать образ llama-server PrismML для bonsai-mtp"
	@echo "make strata      — Qwen3.8-Flash-Next 125B MoE (Strata, IQ2_XS) на двух GPU, порт :8090"
	@echo "make strata-build / logs-strata — собрать образ Strata (исходники в strata/src) / логи"
	@echo "make sdxl        — SDXL на одной GPU, API/UI :8082"
	@echo "make sdxl-dual   — SDXL на двух GPU, API/UI :8082"
	@echo "make sdxl-build  — собрать образ SDXL"
	@echo "make sdxl-download — скачать закрепленные веса SDXL"
	@echo "make sdxl-test   — тесты SDXL без GPU"
	@echo "make models-stop — остановить все модели, освободить VRAM"
	@echo "make status      — показать состояние сервисов"
	@echo "make logs-qwen   — смотреть логи Qwen"
	@echo "make logs-qwen-mtp — смотреть логи Qwen MTP"
	@echo "make logs-bonsai-mtp — смотреть логи Bonsai MTP"
	@echo "make stop        — остановить весь стек"
	@echo "make down        — удалить контейнеры и сеть, сохранить volumes"

infra:
	$(COMPOSE) up -d $(INFRA)

qwen: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen-mtp bonsai-mtp sdxl qwen-image qwen-image-uc strata
	$(COMPOSE) --profile qwen up -d --no-deps qwen
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen запускается: http://127.0.0.1:8080"
	@echo "Готовность: curl --fail http://127.0.0.1:8080/health"

qwen-mtp: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen bonsai-mtp sdxl qwen-image qwen-image-uc strata
	$(COMPOSE) --profile qwen-mtp up -d --no-deps qwen-mtp
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen MTP запускается: http://127.0.0.1:8081"
	@echo "Готовность: curl --fail http://127.0.0.1:8081/health"
	@echo "MTP активен, если в логах есть 'creating MTP draft context' и статистика draft acceptance"

bonsai-mtp-build:
	$(COMPOSE) --profile bonsai-mtp build bonsai-mtp

bonsai-mtp: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp sdxl qwen-image qwen-image-uc strata
	$(COMPOSE) --profile bonsai-mtp up -d --no-deps bonsai-mtp
	@$(MAKE) --no-print-directory images-sync
	@echo "Bonsai MTP запускается: http://127.0.0.1:8089"
	@echo "Готовность: curl --fail http://127.0.0.1:8089/health"

strata-build:
	$(COMPOSE) --profile strata build strata

strata: infra
	mkdir -p models/strata/config
	@[ -e models/strata/config/strata-iq2_xs.shared-settings.json ] || echo '{"reasoning_effort": "low"}' > models/strata/config/strata-iq2_xs.shared-settings.json
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image qwen-image-uc
	$(COMPOSE) --profile strata up -d --no-deps strata
	@$(MAKE) --no-print-directory images-sync
	@echo "Strata запускается: http://127.0.0.1:8090 (первый старт качает ~76 GB: make logs-strata)"
	@echo "Готовность: curl --fail http://127.0.0.1:8090/health"

sdxl-build:
	$(COMPOSE) --profile sdxl build sdxl

sdxl-download:
	bash download-sdxl-model.sh $(COMPOSE)

sdxl-test:
	docker run --rm --network none local/ml-sdxl:1 python -m unittest discover -s tests -v

sdxl: infra
	mkdir -p outputs/sdxl
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp qwen-image qwen-image-uc strata
	$(COMPOSE) --profile sdxl up -d --no-deps sdxl
	@$(MAKE) --no-print-directory images-sync
	@echo "SDXL: http://127.0.0.1:8082"

sdxl-dual: infra
	mkdir -p outputs/sdxl
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp qwen-image qwen-image-uc strata
	$(SDXL_COMPOSE) --profile sdxl up -d --no-deps sdxl
	@$(MAKE) --no-print-directory images-sync
	@echo "SDXL dual: http://127.0.0.1:8082"

qwen-image-uc-build:
	$(COMPOSE) --profile qwen-image-uc build qwen-image-uc

qwen-image-uc-download:
	python3 download-qwen-image.py --profile qwen-image-uc

qwen-image-uc: infra
	mkdir -p outputs/qwen-image-uc
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image strata
	$(COMPOSE) --profile qwen-image-uc up -d --no-deps qwen-image-uc
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen-Image UC: http://127.0.0.1:8085"

# Both services on both GPUs, everything resident in VRAM; measured budget in QWEN-IMAGE-UC-RU.md.
UC_BONSAI_ENV := QWEN_IMAGE_UC_SHARED=1 BONSAI_MTP_CTX_SIZE=24576 BONSAI_MTP_PARALLEL=1 \
	BONSAI_MTP_TENSOR_SPLIT=0.72,0.28 BONSAI_MTP_UBATCH_SIZE=256

uc-bonsai: infra
	mkdir -p outputs/qwen-image-uc
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp sdxl qwen-image strata
	$(UC_BONSAI_ENV) $(COMPOSE) --profile qwen-image-uc --profile bonsai-mtp up -d --no-deps qwen-image-uc bonsai-mtp
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen-Image UC: http://127.0.0.1:8085, Bonsai MTP: http://127.0.0.1:8089"

qwen-image-uc-test:
	python3 -m unittest discover -s qwen-image-uc/tests -v
	$(COMPOSE) --profile qwen-image-uc exec -T qwen-image-uc python /opt/qwen-image-uc/smoke.py

qwen-image-uc-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f sdxl/tests/Dockerfile.browser sdxl/tests
	mkdir -p outputs/qwen-image-uc/screenshots
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_qwen-image-uc \
	  -v $(CURDIR)/qwen-image-uc/tests/browser.cjs:/test/browser.cjs:ro \
	  -v $(CURDIR)/qwen-image-uc/workflows/qwen-image21-uc.ui.json:/workflow.json:ro \
	  -v $(CURDIR)/outputs/qwen-image-uc/screenshots:/screenshots \
	  local/sdxl-browser-test:1

logs-qwen-image-uc:
	$(COMPOSE) logs --tail=100 -f qwen-image-uc

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
	  -v $(CURDIR)/qwen-image/workflows:/workflows:ro local/ml-tgbot:1 python -m unittest discover -s tests -v

tgbot-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f sdxl/tests/Dockerfile.browser sdxl/tests
	mkdir -p outputs/tgbot/screenshots
	TGBOT_ADMIN_KEY=$$(sed -n 's/^TGBOT_ADMIN_KEY=//p' .env) \
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_default -e TGBOT_ADMIN_KEY \
	  -v $(CURDIR)/tgbot/tests/browser.cjs:/test/browser.cjs:ro \
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

# Open WebUI + Open Terminal + MCP images: not models, so model switching and models-stop leave them running,
# except images / images-uc, which only front the image generators and follow them (images-sync).
IMAGES_SERVICES := images images-uc
AGENT_SERVICES := agent-webui agent-terminal $(IMAGES_SERVICES) blender-mcp bili headroom
BLENDER_VERSION ?= 4.0
# Skill directories mounted into the terminal; missing ones are created so the bind mounts work.
AGENT_SKILL_DIRS := $(HOME)/.claude/skills $(HOME)/.agents/skills $(HOME)/.config/opencode/skills \
  $(HOME)/.pi/skills $(HOME)/.pi/agent/skills $(HOME)/.pi/agent/npm/node_modules

agent-key:
	@touch .env
	@[ ! -s .env ] || [ -z "$$(tail -c1 .env)" ] || echo >> .env
	@for name in AGENT_SECRET_KEY AGENT_TERMINAL_KEY AGENT_ADMIN_PASSWORD; do \
	  grep -q "^$$name=" .env && echo "$$name already set in .env" || \
	  { printf '%s=%s\n' "$$name" "$$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> .env; \
	    echo "$$name added to .env"; }; done
	@grep -q '^AGENT_ADMIN_EMAIL=' .env || { echo 'AGENT_ADMIN_EMAIL=admin@ml.local' >> .env; echo "AGENT_ADMIN_EMAIL added to .env"; }

agent-build:
	$(COMPOSE) --profile agent build images blender-mcp bili headroom
	$(COMPOSE) --profile agent pull agent-webui agent-terminal

agent: infra
	@grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env || { echo "Add TGBOT_ADMIN_KEY to .env: make tgbot-key"; exit 1; }
	@for name in AGENT_SECRET_KEY AGENT_TERMINAL_KEY AGENT_ADMIN_EMAIL AGENT_ADMIN_PASSWORD; do grep -q "^$$name=." .env || { echo "Add $$name to .env: make agent-key"; exit 1; }; done
	mkdir -p outputs/agent/webui outputs/agent/terminal outputs/agent/bili outputs/agent/headroom $(AGENT_SKILL_DIRS)
	$(COMPOSE) --profile agent up -d --no-deps $(filter-out $(IMAGES_SERVICES),$(AGENT_SERVICES))
	@$(MAKE) --no-print-directory images-sync
	@echo "Open WebUI: http://$$(hostname -I | awk '{print $$1}'):1098 (вход: AGENT_ADMIN_EMAIL / AGENT_ADMIN_PASSWORD из .env)"

# Open WebUI filters from agent/functions: Billion context and Headroom toggles, Context usage (run once, and after edits).
agent-functions:
	$(COMPOSE) --profile agent exec agent-webui python3 /opt/agent/functions/install.py /opt/agent/functions

# Re-link the host skills after installing or removing some (restarts the terminal only).
agent-skills:
	$(COMPOSE) --profile agent restart agent-terminal
	$(COMPOSE) --profile agent logs --no-log-prefix agent-terminal | grep -E '^skill|skills linked' | tail -60

agent-stop:
	$(COMPOSE) --profile agent stop $(AGENT_SERVICES)

logs-agent:
	$(COMPOSE) --profile agent logs --tail=100 -f $(AGENT_SERVICES)

# The addon file comes from the blender-mcp image, so addon and server versions always match.
blender-addon:
	@appdata=$$(wslpath "$$(powershell.exe -NoProfile -Command '$$env:APPDATA' | tr -d '\r')") && \
	  dir="$$appdata/Blender Foundation/Blender/$(BLENDER_VERSION)/scripts/addons" && mkdir -p "$$dir" && \
	  docker run --rm --entrypoint python local/ml-blender-mcp:1 -c \
	    "import blender_mcp, pathlib; print((pathlib.Path(blender_mcp.__file__).parent / 'bundled' / 'addon.py').read_text(), end='')" \
	    > "$$dir/blender_mcp.py" && echo "Addon: $$dir/blender_mcp.py"
	@echo "Blender: Edit > Preferences > Add-ons > включить 'MCP for Blender'; 3D View > N > MCP for Blender > Start"

# images runs while sdxl or qwen-image runs, images-uc while any of sdxl / qwen-image / qwen-image-uc does.
images-sync:
	@running=" $$($(COMPOSE) $(MODEL_PROFILES) ps --status running --services | tr '\n' ' ') "; up=; down=; \
	case "$$running" in *" sdxl "*|*" qwen-image "*) up=images;; *) down=images;; esac; \
	case "$$running" in *" sdxl "*|*" qwen-image "*|*" qwen-image-uc "*) up="$$up images-uc";; *) down="$$down images-uc";; esac; \
	if [ -n "$$down" ]; then $(COMPOSE) --profile agent stop $$down; fi; \
	if [ -z "$$up" ]; then :; \
	elif grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env 2>/dev/null; then $(COMPOSE) --profile agent up -d --no-deps $$up; \
	else echo "Not starting$$up: add TGBOT_ADMIN_KEY to .env (make tgbot-key)"; fi

images-build:
	$(COMPOSE) --profile agent build images

images-test:
	docker run --rm --network none local/ml-images:1 python -m unittest discover -s tests -v

qwen-image-build:
	$(COMPOSE) --profile qwen-image build qwen-image

qwen-image-download:
	python3 download-qwen-image.py

qwen-image-download-unsloth:
	python3 download-qwen-image.py --variant unsloth-q8_0

qwen-image: infra
	mkdir -p outputs/qwen-image
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image-uc strata
	QWEN_IMAGE_DIT=$(QWEN_IMAGE_DIT) $(COMPOSE) --profile qwen-image up -d --no-deps qwen-image
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen-Image ($(QWEN_IMAGE_DIT)): http://127.0.0.1:8083"

# The DiT is fixed per process; changing it recreates the container.
qwen-image-unsloth:
	$(MAKE) qwen-image QWEN_IMAGE_DIT=unsloth-q8_0

qwen-image-test:
	python3 -m unittest discover -s qwen-image/tests -v
	$(COMPOSE) --profile qwen-image exec -T qwen-image python /opt/qwen-image/smoke.py

qwen-image-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f sdxl/tests/Dockerfile.browser sdxl/tests
	mkdir -p outputs/qwen-image/screenshots
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_default \
	  -v $(CURDIR)/qwen-image/tests/browser.cjs:/test/browser.cjs:ro \
	  -v $(CURDIR)/qwen-image/workflows/qwen-image21.ui.json:/workflow.json:ro \
	  -v $(CURDIR)/outputs/qwen-image/screenshots:/screenshots \
	  local/sdxl-browser-test:1

models-stop:
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image qwen-image-uc strata
	@$(MAKE) --no-print-directory images-sync

status:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent ps -a

logs-qwen:
	$(COMPOSE) logs --tail=100 -f qwen

logs-qwen-mtp:
	$(COMPOSE) logs --tail=100 -f qwen-mtp

logs-bonsai-mtp:
	$(COMPOSE) logs --tail=100 -f bonsai-mtp

logs-strata:
	$(COMPOSE) logs --tail=100 -f strata

logs-sdxl:
	$(COMPOSE) logs --tail=100 -f sdxl

logs-qwen-image:
	$(COMPOSE) logs --tail=100 -f qwen-image

stop:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent stop -t 75

down:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent down
