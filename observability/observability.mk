# Monitoring (included from ../Makefile; recipes run from the repository root).
INFRA := prometheus grafana tempo otel gpu-exporter loki alloy

.PHONY: infra

help-observability:
	@echo "== Мониторинг (observability/)"
	@echo "make infra       — запустить инфраструктуру без моделей"

infra:
	$(COMPOSE) up -d $(INFRA)
