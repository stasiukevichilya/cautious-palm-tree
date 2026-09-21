.DEFAULT_GOAL := help

# Не допускаем одновременного выполнения целей через make -j.
.NOTPARALLEL:

COMPOSE := docker compose
INFRA := prometheus grafana tempo otel gpu-exporter loki alloy

.PHONY: help infra qwen gemma models-stop status logs-qwen logs-gemma stop down

help:
	@echo "make infra       — запустить инфраструктуру без моделей"
	@echo "make qwen        — переключиться на Qwen"
	@echo "make gemma       — переключиться на Gemma"
	@echo "make models-stop — остановить обе модели, освободить VRAM"
	@echo "make status      — показать состояние сервисов"
	@echo "make logs-qwen   — смотреть логи Qwen"
	@echo "make logs-gemma  — смотреть логи Gemma"
	@echo "make stop        — остановить весь стек"
	@echo "make down        — удалить контейнеры и сеть, сохранить volumes"

infra:
	$(COMPOSE) up -d $(INFRA)

qwen: infra
	$(COMPOSE) --profile gemma stop -t 60 gemma
	$(COMPOSE) --profile qwen up -d --no-deps qwen
	@echo "Qwen запускается: http://127.0.0.1:8080"
	@echo "Готовность: curl --fail http://127.0.0.1:8080/health"

gemma: infra
	$(COMPOSE) --profile qwen stop -t 60 qwen
	$(COMPOSE) --profile gemma up -d --no-deps gemma
	@echo "Gemma запускается: http://127.0.0.1:8081"
	@echo "Готовность: curl --fail http://127.0.0.1:8081/health"

models-stop:
	$(COMPOSE) --profile qwen --profile gemma stop -t 60 qwen gemma

status:
	$(COMPOSE) --profile qwen --profile gemma ps -a

logs-qwen:
	$(COMPOSE) logs --tail=100 -f qwen

logs-gemma:
	$(COMPOSE) logs --tail=100 -f gemma

stop:
	$(COMPOSE) --profile qwen --profile gemma stop -t 60

down:
	$(COMPOSE) --profile qwen --profile gemma down
