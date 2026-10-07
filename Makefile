.DEFAULT_GOAL := help

# Не допускаем одновременного выполнения целей через make -j.
.NOTPARALLEL:

# One Compose project (compose.yaml includes the group files); targets live in the group *.mk files.
# Recipes always run from this directory.
COMPOSE := docker compose
MODEL_PROFILES := --profile qwen --profile qwen-mtp --profile bonsai-mtp --profile sdxl --profile qwen-image --profile qwen-image-uc --profile strata

# Order matters: agent.mk uses IMAGES_SERVICES from imagegen.mk.
include observability/observability.mk
include llm/llm.mk
include imagegen/imagegen.mk
include agent/agent.mk
include services/services.mk

.PHONY: help help-common models-stop status stop down

help: help-llm help-imagegen help-agent help-services help-observability help-common

help-common:
	@echo "== Весь стек"
	@echo "make models-stop — остановить все модели, освободить VRAM"
	@echo "make status      — показать состояние сервисов"
	@echo "make stop        — остановить весь стек"
	@echo "make down        — удалить контейнеры и сеть, сохранить volumes"

models-stop:
	$(COMPOSE) $(MODEL_PROFILES) stop -t 75 qwen qwen-mtp bonsai-mtp sdxl qwen-image qwen-image-uc strata
	@$(MAKE) --no-print-directory images-sync

status:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent ps -a

stop:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent stop -t 75

down:
	$(COMPOSE) $(MODEL_PROFILES) --profile agent down
