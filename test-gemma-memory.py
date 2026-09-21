import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8081"
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 32768

# Не используем системный HTTP-прокси для локального сервера.
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, body):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return http.open(request, timeout=3600)


def count_tokens(text):
    with post("/tokenize", {"content": text}) as response:
        return len(json.load(response)["tokens"])


# Уникальное начало уменьшает возможность повторного использования
# префикса предыдущего теста.
prefix = f"Memory benchmark {time.time_ns()}\n"
instruction = """
Ниже находится журнал измерений.
После журнала составь подробный отчет из 100 нумерованных пунктов:
опиши закономерности, возможные проверки и ограничения этих данных.
Не ограничивайся кратким ответом.

"""
block = (
    "Измерение: температура 42 градуса, давление 1013 гПа, "
    "скорость потока 12 метров в секунду. "
    "Датчик исправен, отклонений не обнаружено.\n"
)
suffix = "\nЖурнал завершен. Теперь напиши подробный отчет.\n"

# Подбираем объем по фактической токенизации.
repeats = max(1, TARGET // count_tokens(block))
for _ in range(5):
    text = prefix + instruction + block * repeats + suffix
    count = count_tokens(text)
    if abs(count - TARGET) <= max(128, TARGET // 100):
        break
    repeats = max(1, int(repeats * TARGET / count))

print(f"Токенов текста: {count:,}")
print("Chat template добавит небольшой объем служебных токенов.")
print("Запрос отправлен. Следи за VRAM во время prefill и генерации.",
      flush=True)

started = time.monotonic()
first_token = None
usage = None
finish_reason = None
chunks = 0

with post("/v1/chat/completions", {
    "model": "gemma4-26b-a4b",
    "messages": [{"role": "user", "content": text}],
    "max_tokens": 2048,
    "stream": True,
    "stream_options": {"include_usage": True},
    "cache_prompt": False,
}) as response:
    for raw in response:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break

        event = json.loads(payload)
        if event.get("usage"):
            usage = event["usage"]

        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content") or delta.get("reasoning_content"):
                chunks += 1
                if first_token is None:
                    first_token = time.monotonic()
                    print(
                        f"Первый текстовый фрагмент через "
                        f"{first_token - started:.1f} с",
                        flush=True,
                    )
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

print(f"Время запроса: {time.monotonic() - started:.1f} с")
print(f"Фрагментов ответа: {chunks}")
print(f"Причина завершения: {finish_reason}")
print("Usage:", json.dumps(usage, ensure_ascii=False))
