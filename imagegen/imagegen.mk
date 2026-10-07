# Image generation (included from ../Makefile; recipes run from the repository root).
SDXL_COMPOSE := $(COMPOSE) -f compose.yaml -f imagegen/compose.sdxl-dual.yaml
QWEN_IMAGE_DIT ?= base
IMAGES_SERVICES := images images-uc

.PHONY: sdxl sdxl-dual sdxl-build sdxl-download sdxl-test logs-sdxl
.PHONY: uc-bonsai qwen-image-uc qwen-image-uc-build qwen-image-uc-download qwen-image-uc-test qwen-image-uc-browser-test logs-qwen-image-uc
.PHONY: qwen-image qwen-image-build qwen-image-download qwen-image-download-unsloth qwen-image-unsloth qwen-image-test qwen-image-browser-test logs-qwen-image
.PHONY: images-sync images-build images-test

help-imagegen:
	@echo "== Генерация изображений (imagegen/), вместо LLM"
	@echo "make sdxl        — SDXL на одной GPU, API/UI :8082"
	@echo "make sdxl-dual   — SDXL на двух GPU, API/UI :8082"
	@echo "make sdxl-build / sdxl-download / sdxl-test — образ, закреплённые веса, тесты без GPU"
	@echo "make qwen-image  - Qwen-Image 2.1 on two GPUs, API/UI :8083"
	@echo "make qwen-image-build / qwen-image-download / qwen-image-test / qwen-image-browser-test"
	@echo "make qwen-image-unsloth - the same with unsloth Q8_0 DiT (make qwen-image-download-unsloth first)"
	@echo "make qwen-image-uc - Qwen-Image 2.1 Uncensored, one GPU + CPU text encoder, UI :8085 (not in the bot)"
	@echo "make qwen-image-uc-build / qwen-image-uc-download / qwen-image-uc-test / qwen-image-uc-browser-test"
	@echo "make uc-bonsai - qwen-image-uc (encoder on GPU1) + bonsai-mtp (24k ctx) together, no CPU offload"
	@echo "make images-sync / images-build / images-test — API/MCP images и images-uc идут за генераторами"

sdxl-build:
	$(COMPOSE) --profile sdxl build sdxl

sdxl-download:
	bash imagegen/scripts/download-sdxl-model.sh $(COMPOSE)

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
	python3 imagegen/scripts/download-qwen-image.py --profile qwen-image-uc

qwen-image-uc: infra
	mkdir -p outputs/qwen-image-uc
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image strata
	$(COMPOSE) --profile qwen-image-uc up -d --no-deps qwen-image-uc
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen-Image UC: http://127.0.0.1:8085"

# Both services on both GPUs, everything resident in VRAM; measured budget in imagegen/QWEN-IMAGE-UC-RU.md.
UC_BONSAI_ENV := QWEN_IMAGE_UC_SHARED=1 BONSAI_MTP_CTX_SIZE=24576 BONSAI_MTP_PARALLEL=1 \
	BONSAI_MTP_TENSOR_SPLIT=0.72,0.28 BONSAI_MTP_UBATCH_SIZE=256

uc-bonsai: infra
	mkdir -p outputs/qwen-image-uc
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp sdxl qwen-image strata
	$(UC_BONSAI_ENV) $(COMPOSE) --profile qwen-image-uc --profile bonsai-mtp up -d --no-deps qwen-image-uc bonsai-mtp
	@$(MAKE) --no-print-directory images-sync
	@echo "Qwen-Image UC: http://127.0.0.1:8085, Bonsai MTP: http://127.0.0.1:8089"

qwen-image-uc-test:
	python3 -m unittest discover -s imagegen/qwen-image-uc/tests -v
	$(COMPOSE) --profile qwen-image-uc exec -T qwen-image-uc python /opt/qwen-image-uc/smoke.py

qwen-image-uc-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f imagegen/sdxl/tests/Dockerfile.browser imagegen/sdxl/tests
	mkdir -p outputs/qwen-image-uc/screenshots
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_qwen-image-uc \
	  -v $(CURDIR)/imagegen/qwen-image-uc/tests/browser.cjs:/test/browser.cjs:ro \
	  -v $(CURDIR)/imagegen/qwen-image-uc/workflows/qwen-image21-uc.ui.json:/workflow.json:ro \
	  -v $(CURDIR)/outputs/qwen-image-uc/screenshots:/screenshots \
	  local/sdxl-browser-test:1

logs-qwen-image-uc:
	$(COMPOSE) logs --tail=100 -f qwen-image-uc

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
	python3 imagegen/scripts/download-qwen-image.py

qwen-image-download-unsloth:
	python3 imagegen/scripts/download-qwen-image.py --variant unsloth-q8_0

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
	python3 -m unittest discover -s imagegen/qwen-image/tests -v
	$(COMPOSE) --profile qwen-image exec -T qwen-image python /opt/qwen-image/smoke.py

qwen-image-browser-test:
	docker build -q -t local/sdxl-browser-test:1 -f imagegen/sdxl/tests/Dockerfile.browser imagegen/sdxl/tests
	mkdir -p outputs/qwen-image/screenshots
	docker run --rm --user $(shell id -u):$(shell id -g) -e HOME=/tmp --network ml-local_default \
	  -v $(CURDIR)/imagegen/qwen-image/tests/browser.cjs:/test/browser.cjs:ro \
	  -v $(CURDIR)/imagegen/qwen-image/workflows/qwen-image21.ui.json:/workflow.json:ro \
	  -v $(CURDIR)/outputs/qwen-image/screenshots:/screenshots \
	  local/sdxl-browser-test:1

logs-sdxl:
	$(COMPOSE) logs --tail=100 -f sdxl

logs-qwen-image:
	$(COMPOSE) logs --tail=100 -f qwen-image
