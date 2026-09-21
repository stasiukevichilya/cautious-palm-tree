.DEFAULT_GOAL := help

# Не допускаем одновременного выполнения целей через make -j.
.NOTPARALLEL:

COMPOSE := docker compose
INFRA := prometheus grafana tempo otel gpu-exporter loki alloy
MODEL_PROFILES := --profile qwen --profile gemma --profile sdxl
SDXL_COMPOSE := $(COMPOSE) -f compose.yaml -f compose.sdxl-dual.yaml

.PHONY: help infra qwen gemma sdxl sdxl-dual sdxl-build sdxl-download sdxl-test models-stop status logs-qwen logs-gemma logs-sdxl stop down

help:
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
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 gemma sdxl
	$(COMPOSE) --profile qwen up -d --no-deps qwen
	@echo "Qwen запускается: http://127.0.0.1:8080"
	@echo "Готовность: curl --fail http://127.0.0.1:8080/health"

gemma: infra
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen sdxl
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
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma
	$(COMPOSE) --profile sdxl up -d --no-deps sdxl
	@echo "SDXL: http://127.0.0.1:8082"

sdxl-dual: infra
	mkdir -p outputs/sdxl
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma
	$(SDXL_COMPOSE) --profile sdxl up -d --no-deps sdxl
	@echo "SDXL dual: http://127.0.0.1:8082"

models-stop:
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen gemma sdxl

status:
	$(COMPOSE) $(MODEL_PROFILES) ps -a

logs-qwen:
	$(COMPOSE) logs --tail=100 -f qwen

logs-gemma:
	$(COMPOSE) logs --tail=100 -f gemma

logs-sdxl:
	$(COMPOSE) logs --tail=100 -f sdxl

stop:
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75

down:
	$(COMPOSE) $(MODEL_PROFILES) down
