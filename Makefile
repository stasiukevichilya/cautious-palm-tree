.DEFAULT_GOAL := help

# Не допускаем одновременного выполнения целей через make -j.
.NOTPARALLEL:

COMPOSE := docker compose
INFRA := prometheus grafana tempo otel gpu-exporter loki alloy
MODEL_PROFILES := --profile qwen --profile gemma --profile sdxl --profile qwen-image
SDXL_COMPOSE := $(COMPOSE) -f compose.yaml -f compose.sdxl-dual.yaml
QWEN_IMAGE_DIT ?= base

.PHONY: help infra qwen gemma sdxl sdxl-dual sdxl-build sdxl-download sdxl-test models-stop status logs-qwen logs-gemma logs-sdxl stop down
.PHONY: qwen-image qwen-image-build qwen-image-download qwen-image-download-unsloth qwen-image-unsloth qwen-image-test qwen-image-browser-test logs-qwen-image

help:
	@echo "make qwen-image  - Qwen-Image 2.1 on two GPUs, API/UI :8083"
	@echo "make qwen-image-build / qwen-image-download / qwen-image-test / qwen-image-browser-test"
	@echo "make qwen-image-unsloth - the same with unsloth Q8_0 DiT (make qwen-image-download-unsloth first)"
	@echo "make infra       — запустить инфраструктуру без моделей"
	@echo "make qwen        — переключиться на Qwen"
	@echo "make gemma       — переключиться на Gemma"
	@echo "make sdxl        — SDXL на одной GPU, API/UI :8082"
	@echo "make sdxl-dual   — SDXL на двух GPU, API/UI :8082"
	@echo "make sdxl-build  — собрать образ SDXL"
	@echo "make sdxl-download — скачать закрепленные веса SDXL"
	@echo "make sdxl-test   — тесты SDXL без GPU"
	@echo "make models-stop — остановить все модели, освободить VRAM"
	@echo "make status      — показать состояние сервисов"
	@echo "make logs-qwen   — смотреть логи Qwen"
	@echo "make logs-gemma  — смотреть логи Gemma"
	@echo "make stop        — остановить весь стек"
	@echo "make down        — удалить контейнеры и сеть, сохранить volumes"

infra:
	$(COMPOSE) up -d $(INFRA)

qwen: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 gemma sdxl qwen-image
	$(COMPOSE) --profile qwen up -d --no-deps qwen
	@echo "Qwen запускается: http://127.0.0.1:8080"
	@echo "Готовность: curl --fail http://127.0.0.1:8080/health"

gemma: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen sdxl qwen-image
	$(COMPOSE) --profile gemma up -d --no-deps gemma
	@echo "Gemma запускается: http://127.0.0.1:8081"
	@echo "Готовность: curl --fail http://127.0.0.1:8081/health"

sdxl-build:
	$(COMPOSE) --profile sdxl build sdxl

sdxl-download:
	bash download-sdxl-model.sh $(COMPOSE)

sdxl-test:
	docker run --rm --network none local/ml-sdxl:1 python -m unittest discover -s tests -v

sdxl: infra
	mkdir -p outputs/sdxl
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma qwen-image
	$(COMPOSE) --profile sdxl up -d --no-deps sdxl
	@echo "SDXL: http://127.0.0.1:8082"

sdxl-dual: infra
	mkdir -p outputs/sdxl
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma qwen-image
	$(SDXL_COMPOSE) --profile sdxl up -d --no-deps sdxl
	@echo "SDXL dual: http://127.0.0.1:8082"

qwen-image-build:
	$(COMPOSE) --profile qwen-image build qwen-image

qwen-image-download:
	python3 download-qwen-image.py

qwen-image-download-unsloth:
	python3 download-qwen-image.py --variant unsloth-q8_0

qwen-image: infra
	mkdir -p outputs/qwen-image
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma sdxl
	QWEN_IMAGE_DIT=$(QWEN_IMAGE_DIT) $(COMPOSE) --profile qwen-image up -d --no-deps qwen-image
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
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma sdxl qwen-image

status:
	$(COMPOSE) $(MODEL_PROFILES) ps -a

logs-qwen:
	$(COMPOSE) logs --tail=100 -f qwen

logs-gemma:
	$(COMPOSE) logs --tail=100 -f gemma

logs-sdxl:
	$(COMPOSE) logs --tail=100 -f sdxl

logs-qwen-image:
	$(COMPOSE) logs --tail=100 -f qwen-image

stop:
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75

down:
	$(COMPOSE) $(MODEL_PROFILES) down
