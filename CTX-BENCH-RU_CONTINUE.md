Продолжаем бенчмарк сжатия контекста (Headroom и billion-context) в стеке ~/mlstuff/local.
Сначала прочитай CTX-BENCH-RU.md: там цель, что уже сделано, промежуточные результаты и шаги
продолжения. Код бенчмарка: agent/bench/ctx_bench.py, agent/bench/run.sh, agent/bench/all.sh.
Сырые результаты: outputs/bench/ctx/.

Задача:
1. Проверь состояние: docker ps, должны работать qwen-mtp и headroom. Убедись, что в
   outputs/bench/ctx нет незавершённых прогонов.
2. Запусти agent/bench/all.sh в фоне: qwen-mtp, затем qwen, затем bonsai-mtp, по 4 маршрута на модель
   (direct, hr, bili, hb). Переключать эти три модели через make разрешаю. Strata не запускай,
   её проверим в самом конце отдельно и только после моего подтверждения.
3. Пока идут прогоны, коротко сообщай о прогрессе после каждого маршрута. Не опрашивай чаще раза
   в 5 минут.
4. По каждой модели сведи результаты в таблицу:
   - промпт на последнем шаге;
   - session.prompt_tokens_total и prompt_seconds_total;
   - wall_s;
   - recall ok (4 вопроса);
   - для bili/hb: число событий compress в outputs/agent/bili/state/billion-context/bili.log
     по run_id.
5. Разберись с открытым вопросом: почему hr не сжимает запросы на вопросы о фактах из сеанса
   (смотри prompt в recall и заголовки x-headroom-tokens-*). Если нужно, прогони вариант с
   HEADROOM_MODE=token.
6. Обнови CTX-BENCH-RU.md: итоговые таблицы, выводы и рекомендацию, что включать по умолчанию
   в Open WebUI, pi и opencode.

Если бенчмарк упрётся в окно модели (HTTP 400) или что-то пойдёт не по плану, остановись и спроси меня.
Не коммить без моей просьбы.
