# Language models (included from ../Makefile; recipes run from the repository root).
.PHONY: qwen qwen-mtp bonsai-mtp bonsai-mtp-build strata strata-build logs-qwen logs-qwen-mtp logs-bonsai-mtp logs-strata

help-llm:
	@echo "== Языковые модели (llm/), одна за раз"
	@echo "make qwen        — запустить Qwen"
	@echo "make qwen-mtp    — Qwen с MTP-спекуляцией (RVN Q4_K_M multilingual mtp), порт :8081; взаимно исключается с make qwen"
	@echo "make bonsai-mtp  — Ternary-Bonsai-2-27B Uncensored PQ2_0 + MTP (форк PrismML llama.cpp), порт :8089"
	@echo "make bonsai-mtp-build — собрать образ llama-server PrismML для bonsai-mtp"
	@echo "make strata      — Qwen3.8-Flash-Next 125B MoE (Strata, IQ2_XS) на двух GPU, порт :8090"
	@echo "make strata-build / logs-strata — собрать образ Strata (исходники в llm/strata/src) / логи"
	@echo "make logs-qwen / logs-qwen-mtp / logs-bonsai-mtp — логи"

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

logs-qwen:
	$(COMPOSE) logs --tail=100 -f qwen

logs-qwen-mtp:
	$(COMPOSE) logs --tail=100 -f qwen-mtp

logs-bonsai-mtp:
	$(COMPOSE) logs --tail=100 -f bonsai-mtp

logs-strata:
	$(COMPOSE) logs --tail=100 -f strata
